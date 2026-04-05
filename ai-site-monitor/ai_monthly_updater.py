#!/usr/bin/env python3
"""
ai_monthly_updater.py
店舗の基本要約（shop_ai_summary）とグラフ用データ（年齢層・料金）を月1回更新する専用スクリプト。

【巡回対象】最大3ページ
- TOPページ（必須）
- 料金ページ: TOPから「料金」系リンクを自動検出、なければURLパターン試行
- 出勤/在籍ページ: TOPから「出勤」「キャスト」系リンクを自動検出、なければURLパターン試行

【データ重視の編集部Review】
抽出した年齢層分布・料金・設備を根拠に、数字を必ず含む客観的な紹介文を生成。

※ shop_today_analysis / shop_today_therapists 等の毎日出勤データは一切送信しない。
"""

import asyncio
import json
import os
import re
import sys
import time
from base64 import b64encode
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

# .env 読み込み
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser, TimeoutError as PlaywrightTimeoutError

# google-genai（Gemini API）
try:
    from google import genai
except ImportError:
    print("ERROR: google-genai がインストールされていません。")
    print("  pip install google-genai")
    sys.exit(1)


# 処理する店舗数（0 で全件。テスト時は 3 等に設定）
CRAWL_LIMIT = 0

# タイムアウト（ミリ秒）
PAGE_TIMEOUT_MS = 30000

# Gemini に渡すテキストの最大文字数
MAX_TEXT_LENGTH = 15000

# 料金・出勤ページのURLパターン（動的検出失敗時のフォールバック）
PRICE_PAGE_PATHS = ["/price/", "/fee/", "/course/", "/menu/", "/料金/", "/price-list/", "/course-list/"]
STAFF_PAGE_PATHS = ["/schedule/", "/staff/", "/cast/", "/therapist/", "/member/", "/出勤/", "/在籍/", "/cast-list/"]

# リンクテキスト・href のキーワード（動的検出用）
PRICE_KEYWORDS = ["料金", "価格", "fee", "price", "course", "menu", "コース", "プラン"]
STAFF_KEYWORDS = ["出勤", "キャスト", "staff", "cast", "在籍", "セラピスト", "therapist", "member", "メンバー"]

# リンク検出で除外する拡張子（ランチメニュー画像等）
EXCLUDED_LINK_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".pdf", ".webp")

# Gemini API レート制限回避の待機時間（秒）
SLEEP_BEFORE_GEMINI = 15

# エリア別60分平均料金（姫路・大阪:12000、加古川:11000、その他:12000）
AREA_AVERAGE_60MIN: Dict[str, int] = {
    "himeji": 12000,
    "姫路": 12000,
    "kakogawa": 11000,
    "加古川": 11000,
    "nihonbashi": 12000,
    "日本橋": 12000,
    "osaka": 12000,
    "大阪": 12000,
}
DEFAULT_AREA_AVERAGE_60MIN = 12000

# 年齢抽出用正規表現（(\d{2})[歳才]|(\d{2})\) 等）
AGE_PATTERNS = [
    r"(\d{2})[歳才]",      # 32歳, 25才
    r"\((\d{2})\)",        # (43)
    r"\((\d{2})歳\)",     # (32歳)
    r"\((\d{2})才\)",     # (25才)
]

# 料金抽出用（60分/60min/基本の直後、5,000〜50,000円の範囲）
PRICE_PATTERNS = [
    r"60\s*分[^\d]*(?:¥|円)?\s*[\d,]*(\d{4,5})",
    r"60分[^\d]*(?:¥|円)?\s*[\d,]*(\d{4,5})",
    r"60min[^\d]*(?:¥|円)?\s*[\d,]*(\d{4,5})",
    r"(?:¥|円)?\s*[\d,]*(\d{4,5})\s*(?:円)?[^\d]*60\s*分",
    r"基本[^\d]*(?:¥|円)?\s*[\d,]*(\d{4,5})",
]


def get_config() -> Dict[str, str]:
    """環境変数から設定を取得"""
    site_url = os.environ.get("WP_SITE_URL", "").rstrip("/")
    user = os.environ.get("WP_USER", "")
    app_password = os.environ.get("WP_APP_PASSWORD", "")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")

    missing = []
    if not site_url:
        missing.append("WP_SITE_URL")
    if not user:
        missing.append("WP_USER")
    if not app_password:
        missing.append("WP_APP_PASSWORD")
    if not gemini_key:
        missing.append("GEMINI_API_KEY")

    if missing:
        print(f"ERROR: 以下の環境変数が未設定です: {', '.join(missing)}")
        sys.exit(1)

    return {
        "site_url": site_url,
        "user": user,
        "app_password": app_password,
        "gemini_key": gemini_key,
    }


def fetch_shops(site_url: str, user: str, app_password: str) -> List[Dict]:
    """REST API で shop 投稿一覧を取得（area_slug 含む）"""
    url = f"{site_url}/wp-json/wp/v2/shop"
    auth_str = f"{user}:{app_password}"
    auth_b64 = b64encode(auth_str.encode()).decode()
    headers = {"Authorization": f"Basic {auth_b64}"}
    params = {"per_page": 100, "_fields": "id,title,official_url,acf,area_slug"}

    all_shops = []
    page = 1

    while True:
        params["page"] = page
        resp = requests.get(url, headers=headers, params=params, timeout=30)

        if resp.status_code != 200:
            print(f"ERROR: API エラー (HTTP {resp.status_code})")
            sys.exit(1)

        data = resp.json()
        if not data:
            break

        all_shops.extend(data)
        if len(data) < params["per_page"]:
            break
        page += 1

    return all_shops


def parse_shop(shop: Dict) -> Optional[Dict]:
    """shop データから post_id, name, official_url, area_slug を抽出"""
    post_id = shop.get("id", "")
    title = shop.get("title", {})
    name = title.get("rendered", "（タイトルなし）") if isinstance(title, dict) else str(title)
    official_url = (
        shop.get("official_url")
        or (shop.get("acf") or {}).get("official_url")
        or ""
    )
    official_url = (official_url or "").strip()
    area_slug = (shop.get("area_slug") or "").strip()

    if not official_url or not str(post_id):
        return None

    return {
        "post_id": post_id,
        "name": name,
        "official_url": official_url,
        "area_slug": area_slug,
    }


def get_area_average_60min(area_slug: str) -> int:
    """エリアスラッグから60分平均料金を取得（姫路・大阪:12000、加古川:11000、その他:12000）"""
    if not area_slug:
        return DEFAULT_AREA_AVERAGE_60MIN
    key = area_slug.lower().strip()
    return AREA_AVERAGE_60MIN.get(key, DEFAULT_AREA_AVERAGE_60MIN)


def _age_to_bucket(age: int) -> str:
    """年齢を7段階バケットに変換"""
    if age <= 19:
        return "age_18_19"
    if age <= 24:
        return "age_20_24"
    if age <= 29:
        return "age_25_29"
    if age <= 34:
        return "age_30_34"
    if age <= 39:
        return "age_35_39"
    if age <= 44:
        return "age_40_44"
    return "age_45_plus"


def extract_age_dist(text: str) -> Dict[str, int]:
    """
    テキストから年齢をすべて抽出し、7段階の age_dist 辞書を作成。
    正規表現 (\d{2})[歳才]|(\d{2})\) 等を使用。
    """
    age_dist: Dict[str, int] = {
        "age_18_19": 0,
        "age_20_24": 0,
        "age_25_29": 0,
        "age_30_34": 0,
        "age_35_39": 0,
        "age_40_44": 0,
        "age_45_plus": 0,
    }
    seen_positions: set = set()
    for pattern in AGE_PATTERNS:
        for m in re.finditer(pattern, text):
            try:
                age = int(m.group(1))
                if 18 <= age <= 99:
                    pos = m.start()
                    if pos not in seen_positions:
                        seen_positions.add(pos)
                        bucket = _age_to_bucket(age)
                        age_dist[bucket] = age_dist.get(bucket, 0) + 1
            except (ValueError, IndexError):
                pass
    return age_dist


def extract_price_60min(text: str) -> Optional[int]:
    """
    「60分」「60min」「基本」等のキーワード直後の 5,000〜50,000円 範囲の数値を抽出。
    取得できなければ None を返す（エラー終了せず処理続行）。
    """
    if not text:
        return None
    candidates: List[int] = []
    for pattern in PRICE_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE | re.DOTALL):
            try:
                raw = m.group(1).replace(",", "")
                val = int(raw)
                if 5000 <= val <= 50000:
                    candidates.append(val)
            except (ValueError, IndexError):
                pass
    return min(candidates) if candidates else None


def merge_age_dist(base: Dict[str, int], add: Dict[str, int]) -> Dict[str, int]:
    """年齢分布をマージ（加算）"""
    result = dict(base)
    for k, v in add.items():
        result[k] = result.get(k, 0) + v
    return result


async def extract_body_text(browser: Browser, url: str) -> Optional[str]:
    """Playwright でページの表示テキスト（body 内）を抽出"""
    try:
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        text = await page.locator("body").inner_text()
        await context.close()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines) if lines else None
    except PlaywrightTimeoutError:
        return None
    except Exception:
        return None


async def extract_page_html(browser: Browser, url: str) -> Optional[str]:
    """Playwright でページのHTMLを取得（リンク検出用）"""
    try:
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        html = await page.content()
        await context.close()
        return html
    except PlaywrightTimeoutError:
        return None
    except Exception:
        return None


def _link_matches_keywords(href: str, text: str, keywords: List[str]) -> bool:
    """リンクの href またはテキストがキーワードに該当するか"""
    combined = f"{href} {text}".lower()
    return any(kw.lower() in combined for kw in keywords)


def discover_links_from_html(html: str, base_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    TOPページのHTMLから「料金」「出勤/キャスト」に該当するリンクを自動検出。
    戻り値: (price_url, staff_url)
    """
    soup = BeautifulSoup(html, "html.parser")
    parsed_base = urlparse(base_url)
    base_netloc = parsed_base.netloc or ""

    price_url: Optional[str] = None
    staff_url: Optional[str] = None

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        text = (a.get_text() or "").strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue

        # 画像・PDF等は除外（ランチメニュー等）
        href_lower = href.lower()
        if any(href_lower.endswith(ext) or f"{ext}?" in href_lower for ext in EXCLUDED_LINK_EXTENSIONS):
            continue

        full_url = urljoin(base_url, href)
        if base_netloc and base_netloc not in full_url:
            continue

        if not price_url and _link_matches_keywords(href, text, PRICE_KEYWORDS):
            price_url = full_url
        if not staff_url and _link_matches_keywords(href, text, STAFF_KEYWORDS):
            staff_url = full_url
        if price_url and staff_url:
            break

    return (price_url, staff_url)


async def fetch_page_or_fallback(
    browser: Browser,
    base_url: str,
    discovered_url: Optional[str],
    fallback_paths: List[str],
) -> Tuple[str, Optional[str]]:
    """
    発見URLがあればそれを取得、なければフォールバックパスを試行。
    戻り値: (テキスト, 使用したURL or None)
    """
    if discovered_url:
        text = await extract_body_text(browser, discovered_url)
        if text and len(text) > 100:
            return (text, discovered_url)

    parsed = urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    base_path = (parsed.path or "").rstrip("/") or ""

    for path in fallback_paths:
        full_path = path if path.startswith("/") else f"/{path}"
        url = urljoin(base, base_path + full_path)
        text = await extract_body_text(browser, url)
        if text and len(text) > 100:
            return (text, url)

    return ("", None)


def _parse_summary_json(raw: str) -> Optional[str]:
    """Gemini の応答から summary をパース"""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "```" in raw:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
        raw = m.group(1).strip() if m else raw
    try:
        data = json.loads(raw)
        summary = (data.get("summary") or "").strip()
        return summary if summary else None
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def generate_monthly_summary(
    text: str,
    gemini_key: str,
    age_dist: Dict[str, int],
    shop_price_60: Optional[int],
    area_avg_60: int,
) -> Optional[str]:
    """
    データアナリスト兼編集者として、解析データに基づく紹介文を生成。
    年齢分布・料金比較結果をプロンプトに注入。
    """
    if not text or len(text.strip()) < 50:
        return None

    truncated = text[:MAX_TEXT_LENGTH] if len(text) > MAX_TEXT_LENGTH else text

    has_price = shop_price_60 is not None and shop_price_60 > 0
    if has_price:
        diff = area_avg_60 - shop_price_60
        if diff > 0:
            price_diff = f"エリア平均より{diff}円安い"
        elif diff < 0:
            price_diff = f"エリア平均より{abs(diff)}円高い"
        else:
            price_diff = "エリア平均と同等"
        shop_price_str = f"{shop_price_60}円 (エリア平均: {area_avg_60}円) → {price_diff}"
    else:
        shop_price_str = "未取得（言及不要）"
        price_diff = ""

    price_extra = ""
    if not has_price:
        price_extra = "\n※店舗の料金データが未取得の場合は、料金に関する言及は避け、年齢層分布やコンセプトのみを根拠にして紹介文を作成してください。"

    summary_prompt = """あなたはデータアナリスト兼編集者です。以下の【解析データ】に基づき、客観的な事実のみを述べてください。

【解析データ】
・年齢層分布: {age_dist}
・当店60分料金: {shop_price}
・設備/コンセプト: (下記スクレイピングテキストから抽出)

[制約]
1. 『〇〇代が中心』『エリア平均より〇〇円安い（または同等）』といった具体的な数字を必ず1つ以上含めること。
2. ポエムや過剰な装飾語は一切使わず、スマートな『です/ます』調で3-4行にまとめること。
3. 絵文字・「！」は禁止。性的表現・風俗連想NGワードは一切使用しないこと。
{price_extra}

以下のJSON形式のみで出力してください。
{{
  "summary": "データ根拠に基づいた3〜4行の紹介文"
}}
""".format(
        age_dist=json.dumps(age_dist, ensure_ascii=False),
        shop_price=shop_price_str,
        price_extra=price_extra,
    )

    prompt = f"{summary_prompt}\n\n【店舗サイトのテキスト】\n{truncated}"

    models = [
        "models/gemini-2.5-flash",
        "models/gemini-2.0-flash",
        "models/gemini-1.5-pro",
        "models/gemini-1.5-flash",
        "models/gemini-flash-latest",
    ]

    for model_name in models:
        try:
            time.sleep(SLEEP_BEFORE_GEMINI)
            client = genai.Client(api_key=gemini_key)
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            raw_resp = (response.text or "").strip()
            if raw_resp:
                summary = _parse_summary_json(raw_resp)
                if summary:
                    return summary
                return raw_resp
            print(f"    [Gemini] モデル {model_name}: 応答が空です")
        except Exception as e:
            print(f"    [Gemini] モデル {model_name} エラー: {type(e).__name__}: {e}")
            continue

    return None


def _build_rest_urls(site_url: str) -> List[str]:
    """REST API の URL 候補を返す"""
    base = site_url.rstrip("/")
    return [
        f"{base}/wp-json/ai-engine/v1/update",
        f"{base}/?rest_route=/ai-engine/v1/update",
    ]


def update_shop_monthly_summary(
    site_url: str,
    user: str,
    app_password: str,
    post_id: int,
    summary: str,
    age_dist: Dict[str, int],
    shop_price_60min: Optional[int],
    area_average_60min: int,
) -> bool:
    """
    WordPress REST API で shop_ai_summary とグラフ用データを一括保存。
    age_18_19 〜 age_45_plus の7項目、shop_price_60min, area_average_60min を含む。
    """
    if not summary or not summary.strip():
        return False

    meta: Dict[str, Any] = {
        "shop_ai_summary": summary.strip(),
        "age_18_19": age_dist.get("age_18_19", 0),
        "age_20_24": age_dist.get("age_20_24", 0),
        "age_25_29": age_dist.get("age_25_29", 0),
        "age_30_34": age_dist.get("age_30_34", 0),
        "age_35_39": age_dist.get("age_35_39", 0),
        "age_40_44": age_dist.get("age_40_44", 0),
        "age_45_plus": age_dist.get("age_45_plus", 0),
        "shop_price_60min": shop_price_60min,
        "area_average_60min": area_average_60min,
    }

    payload = {
        "shop_post_id": post_id,
        "meta": meta,
        "summary": summary.strip(),
        "log_type": "monthly",
    }
    auth = (user, app_password)
    urls = _build_rest_urls(site_url)

    for url in urls:
        try:
            resp = requests.post(url, json=payload, auth=auth, timeout=30)
            if resp.status_code in (200, 201):
                return True
            if resp.status_code == 404:
                continue
            print(f"    [WordPress] 保存失敗 (URL: {url}): status_code={resp.status_code}")
            return False
        except Exception as e:
            print(f"    [WordPress] リクエスト例外 ({url}): {type(e).__name__}: {e}")
            continue

    print(f"    [WordPress] 保存失敗: 全 URL で 404")
    return False


async def process_shop(
    browser: Browser,
    config: Dict[str, str],
    shop: Dict,
    index: int,
) -> None:
    """1店舗の巡回（最大3ページ）・データ抽出・要約・更新を実行。エラー時はログ出力して次へ進む"""
    post_id = shop["post_id"]
    name = shop["name"]
    base_url = shop["official_url"]
    area_slug = shop.get("area_slug", "")

    try:
        area_avg_60 = get_area_average_60min(area_slug)

        print(f"\n[{index}] {name} (post_id: {post_id})")
        print(f"    URL: {base_url}")
        print(f"    エリア: {area_slug or '未設定'} → 60分平均 {area_avg_60}円")

        # 1. TOPページ（必須）
        main_text = await extract_body_text(browser, base_url)
        if not main_text:
            print("    ERROR: TOPページのテキスト抽出に失敗（次の店舗へ進みます）")
            return

        print(f"    TOP: {len(main_text)} 文字")

        age_dist = extract_age_dist(main_text)

        # 2. 動的リンク検出
        html = await extract_page_html(browser, base_url)
        price_url_detected: Optional[str] = None
        staff_url_detected: Optional[str] = None
        if html:
            price_url_detected, staff_url_detected = discover_links_from_html(html, base_url)
            if price_url_detected:
                print(f"    料金リンク検出: {price_url_detected[:60]}...")
            if staff_url_detected:
                print(f"    出勤リンク検出: {staff_url_detected[:60]}...")

        # 3. 料金ページ（動的 or フォールバック）※取得できなくても処理続行
        price_text, _ = await fetch_page_or_fallback(
            browser, base_url, price_url_detected, PRICE_PAGE_PATHS
        )
        shop_price_60: Optional[int] = None
        if price_text:
            shop_price_60 = extract_price_60min(price_text)
            if shop_price_60:
                print(f"    料金: 60分 {shop_price_60}円 を検出")
            else:
                shop_price_60 = extract_price_60min(main_text)
                if shop_price_60:
                    print(f"    料金: TOPから60分 {shop_price_60}円 を検出")
        if not shop_price_60:
            shop_price_60 = extract_price_60min(main_text)
            if shop_price_60:
                print(f"    料金: TOPから60分 {shop_price_60}円 を検出")
            else:
                print("    料金: 検出できず（紹介文は年齢層・コンセプトのみで作成）")

        # 4. 出勤/在籍ページ（動的 or フォールバック）
        staff_text, _ = await fetch_page_or_fallback(
            browser, base_url, staff_url_detected, STAFF_PAGE_PATHS
        )
        if staff_text:
            print(f"    出勤表: 取得 ({len(staff_text)} 文字)")
            age_dist = merge_age_dist(age_dist, extract_age_dist(staff_text))
        else:
            print("    出勤表: 該当ページなし")

        print(f"    年齢層: {age_dist}")

        # 5. 編集部Review生成（年齢分布・料金比較を注入）
        summary = generate_monthly_summary(
            main_text,
            config["gemini_key"],
            age_dist,
            shop_price_60,
            area_avg_60,
        )
        if not summary:
            print("    ERROR: Gemini 要約に失敗（次の店舗へ進みます）")
            return

        print(f"    紹介文: {len(summary)} 文字")
        preview = summary[:120] + "..." if len(summary) > 120 else summary
        print(f"    ---\n    {preview}")

        # 6. WordPress に一括保存
        success = update_shop_monthly_summary(
            config["site_url"],
            config["user"],
            config["app_password"],
            int(post_id),
            summary,
            age_dist,
            shop_price_60,
            area_avg_60,
        )

        if success:
            print("    ✓ 更新完了")
        else:
            print("    ERROR: WordPress への保存に失敗")
    except Exception as e:
        print(f"    ERROR: 処理中に例外が発生しました（次の店舗へ進みます）: {type(e).__name__}: {e}")


async def main_async() -> None:
    config = get_config()
    shops = fetch_shops(
        config["site_url"],
        config["user"],
        config["app_password"],
    )

    valid_shops = []
    for shop in shops:
        parsed = parse_shop(shop)
        if parsed:
            valid_shops.append(parsed)
            if CRAWL_LIMIT > 0 and len(valid_shops) >= CRAWL_LIMIT:
                break

    if not valid_shops:
        print("ERROR: URL が有効な店舗が 1 件もありません。")
        sys.exit(1)

    total = len(valid_shops)
    print(f"処理対象: {total} 件（月1回強制更新・最大3ページ/店舗）")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for i, shop in enumerate(valid_shops, 1):
            await process_shop(browser, config, shop, i)
            time.sleep(SLEEP_BEFORE_GEMINI)  # 店舗間のレートリミット回避

        await browser.close()

    print(f"\n--- 完了（{total} 件処理）---")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
