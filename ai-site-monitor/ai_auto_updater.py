#!/usr/bin/env python3
"""
ai_auto_updater.py
WordPress から店舗リストを取得し、Playwright で公式サイトを巡回、
Gemini API で本日出勤データを抽出し、shop_today_* に自動保存する。

※ shop_ai_summary（月1回の基本要約）は上書きしない。
【テスト仕様】URL が有効な店舗のうち、最初の 3 件のみ処理
"""

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


# テスト用：処理する店舗数
CRAWL_LIMIT = 3

# SQLite DB パス（スクリプトと同じ階層）
SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "escomi_crawler.db"

# タイムアウト（ミリ秒）
PAGE_TIMEOUT_MS = 30000

# Gemini に渡すテキストの最大文字数（トークン節約）
MAX_TEXT_LENGTH = 15000

# 要約プロンプト（本日出勤データの分析のみ・店舗紹介は絶対に書かない）
SUMMARY_PROMPT = """あなたはメンズエステ特化型ポータルサイトのプロのSEOライターです。与えられたテキストから「本日の出勤スケジュール」のみを抽出し、必ず以下のJSON形式のみで返してください（余計な説明やマークダウンは不要）。

【出力形式】必ずこのJSON形式のみ:
{
  "today_analysis": "本日出勤の分析コメント（1〜2行）",
  "availability": "すぐご案内可能、本日空きあり等の記載がテキスト内にあればその文言を抽出。無ければ空文字",
  "today_therapists": [
    {"name": "ゆな", "time": "12:00-LAST", "tags": ["新人"]}
  ],
  "ages": {"18": 0, "20": 0, "25": 0, "30": 0, "35": 0, "40": 0}
}

【today_analysis】与えられたテキストから本日の出勤スケジュールを抽出し、新人やレア出勤がいれば熱くピックアップする1〜2行のコメントを作成せよ。例：「今日は新人セラピストの〇〇ちゃんが出勤！レア出勤の〇〇ちゃんを予約するチャンスです！」。一般的な店舗のコンセプト・紹介・雰囲気は絶対に書かないこと。出勤データが無ければ空文字 ""。

【availability】「すぐご案内可能」「本日空きあり」「最短〇〇分でご案内」等があればその文言を抜き出す。無ければ ""。

【today_therapists】本日の出勤キャスト・セラピスト情報がテキストにあれば抽出。各要素: name（名前）, time（出勤時間帯）, tags（新人・レア・指名等のタグ配列）。無ければ空配列 []。

【ages】在籍セラピストの年齢層分布。18〜19歳→"18"、20〜24歳→"20"、25〜29歳→"25"、30〜34歳→"30"、35〜39歳→"35"、40〜44歳→"40"。テキストから人数を推定して整数で。不明なら0。

【絶対厳守】店舗の紹介・コンセプト・雰囲気は絶対に書かないこと。本日の出勤データのみに特化すること。SEOペナルティ回避のため、性的表現・風俗連想NGワードは一切使用しないこと。

※注意：出力する分析コメント(today_analysis)には、絵文字（✨や🔰など）を一切使用せず、上品で落ち着いたトーンのテキストにすること。"""

# ルートC用：出勤情報のみ生成（today_therapists は渡したものをそのまま返す）
SUMMARY_PROMPT_ANALYSIS_ONLY = """あなたはメンズエステ特化型ポータルサイトのプロのSEOライターです。以下に「すでに抽出済みの本日出勤データ」がJSON形式で渡されます。あなたの仕事は以下2点のみです。

1. today_analysis: 渡された出勤データをもとに、新人やレア出勤がいれば熱くピックアップする1〜2行のコメントを作成せよ。例：「今日は新人セラピストの〇〇ちゃんが出勤！レア出勤の〇〇ちゃんを予約するチャンスです！」
2. availability: 渡されたデータ内に「すぐご案内可」「本日空きあり」等の記載があればその文言を抜き出す。無ければ ""。

【絶対厳守】today_therapists は渡されたJSONをそのまま返すこと。変更・追加・削除しないこと。ages はすべて0で返すこと。today_analysis には絵文字を一切使用しないこと。上品で落ち着いたトーンにすること。

【出力形式】必ずこのJSON形式のみ:
{
  "today_analysis": "1〜2行の分析コメント",
  "availability": "空き状況の文言または空文字",
  "today_therapists": [渡された配列をそのまま],
  "ages": {"18": 0, "20": 0, "25": 0, "30": 0, "35": 0, "40": 0}
}"""

# ドメインごとのスクレイピングルール（後から店舗ごとに追加しやすい構造）
SCRAPING_RULES: Dict[str, Dict[str, str]] = {
    # ダミー例: example.com 用（実際の店舗ルールは後で追加）
    "example.com": {
        "cast_item": ".cast-item",           # 各キャストの親要素
        "cast_name": ".cast-name",           # 名前
        "cast_time": ".cast-time",           # 出勤時間
        "cast_status": ".cast-status",       # 空き状況（すぐご案内可、満了など）
    },
    # 汎用フォールバック（多くのサイトで試す共通パターン）
    "_default": {
        "cast_item": "[class*='cast'] [class*='item'], [class*='therapist'] [class*='card'], .schedule-item",
        "cast_name": "[class*='name'], .therapist-name, .cast-name",
        "cast_time": "[class*='time'], .work-time, .schedule-time",
        "cast_status": "[class*='status'], [class*='availability'], .vacancy",
    },
}


def _get_domain(url: str) -> str:
    """URL からドメインを取得"""
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc or ""
        return netloc.lower().replace("www.", "")
    except Exception:
        return ""


def extract_schedule_from_html(html: str, url: str) -> List[Dict[str, Any]]:
    """
    BeautifulSoup で HTML から本日の出勤キャストを抽出（ルートC・非AI）
    戻り値: [{"name": "ゆな", "time": "12:00-LAST", "status": "予約満了", "tags": []}, ...]
    """
    if not html or not html.strip():
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
        domain = _get_domain(url)
        rules = SCRAPING_RULES.get(domain) or SCRAPING_RULES.get("_default")
        if not rules:
            return []

        # 複数セレクタを試す（カンマ区切りで分割）
        cast_item_sel = rules.get("cast_item", "")
        cast_name_sel = rules.get("cast_name", "")
        cast_time_sel = rules.get("cast_time", "")
        cast_status_sel = rules.get("cast_status", "")

        if not cast_item_sel:
            return []

        items = soup.select(cast_item_sel)
        if not items:
            return []

        result = []
        for item in items:
            name_el = item.select_one(cast_name_sel) if cast_name_sel else None
            time_el = item.select_one(cast_time_sel) if cast_time_sel else None
            status_el = item.select_one(cast_status_sel) if cast_status_sel else None

            name = (name_el.get_text(strip=True) if name_el else "").strip()
            time_val = (time_el.get_text(strip=True) if time_el else "").strip()
            status_val = (status_el.get_text(strip=True) if status_el else "").strip()

            if not name:
                continue

            result.append({
                "name": name,
                "time": time_val or "",
                "status": status_val or "",
                "tags": [],
            })
        return result
    except Exception:
        return []


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


def init_db() -> None:
    """SQLite DB を初期化し、shop_logs と therapist_logs テーブルを作成"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shop_logs (
                shop_id INTEGER PRIMARY KEY,
                last_hash TEXT NOT NULL,
                last_updated DATETIME NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS therapist_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                date TEXT NOT NULL,
                UNIQUE(shop_id, name, date)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_therapist_logs_lookup ON therapist_logs(shop_id, name, date)")
        conn.commit()


def compute_hash(text: str) -> str:
    """テキストの SHA256 ハッシュを返す"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_last_hash(shop_id: int) -> Optional[str]:
    """shop_id の last_hash を取得。存在しなければ None"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT last_hash FROM shop_logs WHERE shop_id = ?",
            (shop_id,),
        )
        row = cur.fetchone()
        return row["last_hash"] if row else None


def update_shop_hash(shop_id: int, new_hash: str) -> None:
    """shop_id の last_hash と last_updated を更新（存在しなければ INSERT）"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO shop_logs (shop_id, last_hash, last_updated)
            VALUES (?, ?, ?)
            ON CONFLICT(shop_id) DO UPDATE SET
                last_hash = excluded.last_hash,
                last_updated = excluded.last_updated
            """,
            (shop_id, new_hash, now),
        )
        conn.commit()


def save_therapist_log(shop_id: int, name: str) -> None:
    """therapist_logs に出勤記録を保存（同日同名は重複しない）"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO therapist_logs (shop_id, name, date)
            VALUES (?, ?, ?)
            """,
            (shop_id, name.strip(), today),
        )
        conn.commit()


def get_therapist_attendance_count(shop_id: int, name: str) -> int:
    """過去30日間の出勤回数を返す。3回以下ならレアとみなす"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            SELECT COUNT(*) FROM therapist_logs
            WHERE shop_id = ? AND name = ? AND date >= ?
            """,
            (shop_id, name.strip(), cutoff),
        )
        row = cur.fetchone()
        return row[0] if row else 0


def apply_rare_tags(shop_id: int, today_therapists: List[Dict]) -> List[Dict]:
    """過去30日間の出勤が3回以下のキャストに「レア」タグを強制追加"""
    result = []
    for t in today_therapists:
        t = dict(t)
        name = (t.get("name") or "").strip()
        tags = list(t.get("tags") or [])
        if name:
            count = get_therapist_attendance_count(shop_id, name)
            if count <= 3 and "レア" not in tags:
                tags.append("レア")
        t["tags"] = tags
        result.append(t)
    return result


def fetch_shops(site_url: str, user: str, app_password: str) -> List[Dict]:
    """REST API で shop 投稿一覧を取得"""
    url = f"{site_url}/wp-json/wp/v2/shop"
    auth_str = f"{user}:{app_password}"
    auth_b64 = b64encode(auth_str.encode()).decode()
    headers = {"Authorization": f"Basic {auth_b64}"}
    params = {"per_page": 100, "_fields": "id,title,official_url,acf"}

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
    """shop データから post_id, name, official_url を抽出"""
    post_id = shop.get("id", "")
    title = shop.get("title", {})
    name = title.get("rendered", "（タイトルなし）") if isinstance(title, dict) else str(title)
    official_url = (
        shop.get("official_url")
        or (shop.get("acf") or {}).get("official_url")
        or ""
    )
    official_url = (official_url or "").strip()

    if not official_url or not str(post_id):
        return None

    return {"post_id": post_id, "name": name, "official_url": official_url}


async def scrape_text_with_playwright(browser: Browser, url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Playwright でページのテキストと生HTMLの両方を取得（ルートC対応）
    戻り値: (text, html) のタプル。失敗時は (None, None)
    """
    try:
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()

        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

        # テキスト（body 内、script/style 除外）
        text = await page.locator("body").inner_text()
        # 生HTML（ルートC用）
        html = await page.content()
        await context.close()

        # テキストの余分な空白を整理
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text_out = "\n".join(lines) if lines else None
        return (text_out, html)
    except PlaywrightTimeoutError:
        return (None, None)
    except Exception:
        return (None, None)


def _parse_gemini_json(raw: str) -> Optional[Dict[str, Any]]:
    """Gemini の応答から全フィールドをパース。失敗時は None"""
    raw = (raw or "").strip()
    if not raw:
        return None

    if "```" in raw:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
        raw = m.group(1).strip() if m else raw

    try:
        data = json.loads(raw)
        today_analysis = (data.get("today_analysis") or data.get("summary") or "").strip()
        availability = (data.get("availability") or "").strip()
        today_therapists = data.get("today_therapists")
        if not isinstance(today_therapists, list):
            today_therapists = []
        ages = data.get("ages")
        if not isinstance(ages, dict):
            ages = {"18": 0, "20": 0, "25": 0, "30": 0, "35": 0, "40": 0}
        for k in ("18", "20", "25", "30", "35", "40"):
            if k not in ages:
                ages[k] = 0
            ages[k] = int(ages[k]) if isinstance(ages[k], (int, float)) else 0
        if not today_analysis and not today_therapists:
            return None
        return {
            "today_analysis": today_analysis,
            "availability": availability,
            "today_therapists": today_therapists,
            "ages": ages,
        }
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def generate_analysis_only_from_therapists(
    therapists_json: str, gemini_key: str
) -> Optional[Dict[str, Any]]:
    """
    ルートC用：出勤データは渡されたものをそのまま使い、today_analysis と availability のみ Gemini で生成
    """
    if not therapists_json or not therapists_json.strip():
        return None

    prompt = f"""{SUMMARY_PROMPT_ANALYSIS_ONLY}

【抽出済みの本日出勤データ（そのまま返すこと）】
{therapists_json}
"""

    models = [
        "models/gemini-2.5-flash",
        "models/gemini-2.0-flash",
        "models/gemini-1.5-pro",
        "models/gemini-1.5-flash",
        "models/gemini-flash-latest",
    ]
    for model_name in models:
        try:
            client = genai.Client(api_key=gemini_key)
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            raw = (response.text or "").strip()
            if raw:
                parsed = _parse_gemini_json(raw)
                if parsed:
                    return parsed
            print(f"    [Gemini] モデル {model_name}: 応答が空です")
        except Exception as e:
            print(f"    [Gemini] モデル {model_name} は利用不可のためスキップ: {type(e).__name__}")
            continue
    return None


def generate_summary_with_gemini(text: str, gemini_key: str) -> Optional[Dict[str, Any]]:
    """Gemini API で要約・空き状況・出勤・年齢層を生成。戻り値は辞書"""
    if not text or len(text.strip()) < 50:
        return None

    truncated = text[:MAX_TEXT_LENGTH] if len(text) > MAX_TEXT_LENGTH else text

    models = [
        "models/gemini-2.5-flash",
        "models/gemini-2.0-flash",
        "models/gemini-1.5-pro",
        "models/gemini-1.5-flash",
        "models/gemini-flash-latest",
    ]

    for model_name in models:
        try:
            client = genai.Client(api_key=gemini_key)
            prompt = f"{SUMMARY_PROMPT}\n\n【店舗サイトのテキスト】\n{truncated}"
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            raw = (response.text or "").strip()
            if raw:
                parsed = _parse_gemini_json(raw)
                if parsed:
                    return parsed
                # JSON パース失敗時は today_analysis を raw で
                return {
                    "today_analysis": raw,
                    "availability": "",
                    "today_therapists": [],
                    "ages": {"18": 0, "20": 0, "25": 0, "30": 0, "35": 0, "40": 0},
                }
            print(f"    [Gemini] モデル {model_name}: 応答が空です")
        except Exception as e:
            print(f"    [Gemini] モデル {model_name} は利用不可のためスキップ: {type(e).__name__}")
            continue

    return None


def _build_rest_urls(site_url: str) -> list:
    """REST API の URL 候補を返す（パーマリンク形式に応じて両方試す）"""
    base = site_url.rstrip("/")
    return [
        f"{base}/wp-json/ai-engine/v1/update",  # パーマリンク「投稿名」等
        f"{base}/?rest_route=/ai-engine/v1/update",  # パーマリンク「基本」の場合
    ]


def update_shop_ai_summary(
    site_url: str,
    user: str,
    app_password: str,
    post_id: int,
    today_analysis: str,
    availability: str = "",
    today_therapists: Optional[List[Dict]] = None,
    ages: Optional[Dict[str, int]] = None,
) -> bool:
    """WordPress REST API で shop_today_analysis, shop_availability, 出勤, 年齢層を更新"""
    avail_value = "" if (not availability or availability.strip().lower() == "なし") else availability.strip()
    meta = {
        "shop_today_analysis": today_analysis,
        "shop_availability": avail_value,
    }
    if today_therapists is not None:
        meta["shop_today_therapists"] = today_therapists
    if ages is not None:
        meta["age_18"] = ages.get("18", 0)
        meta["age_20"] = ages.get("20", 0)
        meta["age_25"] = ages.get("25", 0)
        meta["age_30"] = ages.get("30", 0)
        meta["age_35"] = ages.get("35", 0)
        meta["age_40"] = ages.get("40", 0)

    payload = {
        "shop_post_id": post_id,
        "meta": meta,
        "summary": today_analysis,
        "log_type": "update",
    }
    auth = (user, app_password)
    urls = _build_rest_urls(site_url)

    for url in urls:
        try:
            resp = requests.post(url, json=payload, auth=auth, timeout=30)
            if resp.status_code in (200, 201):
                return True
            if resp.status_code == 404:
                continue  # 次の URL 形式を試す
            # 404 以外の失敗
            print(f"    [WordPress] 保存失敗 (URL: {url}):")
            print(f"      status_code: {resp.status_code}")
            print(f"      response.text:\n{resp.text if resp.text else '(空)'}")
            return False
        except Exception as e:
            print(f"    [WordPress] リクエスト例外 ({url}): {type(e).__name__}: {e}")
            continue

    # 全 URL で 404
    print(f"    [WordPress] 保存失敗: 全 URL で 404 (rest_no_route)")
    print(f"      試した URL: {urls}")
    print(f"      → パーマリンクの更新（設定→パーマリンク→保存）を実行してください")
    return False


async def process_shop(
    browser: Browser,
    config: Dict[str, str],
    shop: Dict,
    index: int,
) -> None:
    """1店舗の巡回・要約・更新を実行"""
    post_id = shop["post_id"]
    name = shop["name"]
    url = shop["official_url"]

    print(f"\n[{index}] {name} (post_id: {post_id})")
    print(f"    URL: {url}")

    # 1. テキスト＋HTML取得（ルートC対応）
    text, html = await scrape_text_with_playwright(browser, url)
    if not text:
        print("    ERROR: テキスト抽出に失敗（タイムアウトまたはエラー）")
        return

    print(f"    テキスト抽出: {len(text)} 文字")

    # 2. 差分検知（Hashチェック）
    current_hash = compute_hash(text)
    last_hash = get_last_hash(int(post_id))

    if last_hash is not None and last_hash == current_hash:
        print("    更新なしのためAI処理をスキップします")
        return

    # 3. ルートC: BeautifulSoup で出勤情報を抽出
    scraped_therapists: List[Dict[str, Any]] = []
    if html:
        scraped_therapists = extract_schedule_from_html(html, url)
        if scraped_therapists:
            print(f"    [ルートC] スクレイピングで {len(scraped_therapists)} 名の出勤情報を抽出")

    # 4. ルート分岐: スクレイピング成功時は分析のみ、失敗時はフル抽出
    if scraped_therapists:
        therapists_json = json.dumps(scraped_therapists, ensure_ascii=False, indent=2)
        result = generate_analysis_only_from_therapists(therapists_json, config["gemini_key"])
        if result:
            result["today_therapists"] = scraped_therapists
    else:
        result = generate_summary_with_gemini(text, config["gemini_key"])

    if not result:
        print("    ERROR: Gemini 要約に失敗（上記のエラー詳細を確認してください）")
        return

    today_analysis = result["today_analysis"]
    availability = result.get("availability", "")
    today_therapists = result.get("today_therapists", [])

    # 出勤キャストを therapist_logs に保存し、過去30日3回以下ならレアタグを追加
    for t in today_therapists:
        name = (t.get("name") or "").strip()
        if name:
            save_therapist_log(int(post_id), name)
    today_therapists = apply_rare_tags(int(post_id), today_therapists)

    ages = result.get("ages", {})

    print(f"    分析コメント: {len(today_analysis)} 文字")
    if availability and availability.strip():
        print(f"    空き状況: {availability}")
    if today_therapists:
        print(f"    出勤キャスト: {len(today_therapists)} 名")
    if any(ages.get(k, 0) for k in ("18", "20", "25", "30", "35", "40")):
        print(f"    年齢層: {ages}")
    preview = today_analysis[:150] + "..." if len(today_analysis) > 150 else today_analysis
    print(f"    ---\n    {preview}")

    # 4. WordPress に保存
    success = update_shop_ai_summary(
        config["site_url"],
        config["user"],
        config["app_password"],
        int(post_id),
        today_analysis,
        availability,
        today_therapists,
        ages,
    )

    if success:
        print("    ✓ WordPress に保存完了")
        update_shop_hash(int(post_id), current_hash)
    else:
        print("    ERROR: WordPress への保存に失敗")


async def main_async() -> None:
    config = get_config()
    shops = fetch_shops(
        config["site_url"],
        config["user"],
        config["app_password"],
    )

    # URL が有効な店舗のみ、最初の CRAWL_LIMIT 件
    valid_shops = []
    for shop in shops:
        parsed = parse_shop(shop)
        if parsed:
            valid_shops.append(parsed)
            if len(valid_shops) >= CRAWL_LIMIT:
                break

    if not valid_shops:
        print("ERROR: URL が有効な店舗が 1 件もありません。")
        sys.exit(1)

    print(f"処理対象: {len(valid_shops)} 件")

    init_db()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for i, shop in enumerate(valid_shops, 1):
            await process_shop(browser, config, shop, i)

        await browser.close()

    print("\n--- 完了 ---")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
