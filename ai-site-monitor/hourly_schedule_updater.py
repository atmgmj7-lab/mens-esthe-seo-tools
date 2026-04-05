#!/usr/bin/env python3
"""
hourly_schedule_updater.py
ハイブリッド方式のAI学習型スクレイパー：1時間に1回の高頻度実行用。

【学習フェーズ】Gemini API 発動条件（この時だけAPI呼び出し）:
  - scraping_rules に該当ドメインのセレクタがない場合
  - または、既存セレクタで試行したが一人もキャストが取得できなかった場合

【高速運用フェーズ】AI非稼働:
  - DBにセレクタがある場合は BeautifulSoup のみで実行（APIコスト0）
  - 名前のクレンジング（(たなか)(42)等の除去）を必ず適用
"""

import json
import os
import random
import re
import sqlite3
import sys
import time
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

# .env 読み込み
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests
from bs4 import BeautifulSoup

# Gemini API（セレクタ学習時のみ使用）
try:
    from google import genai
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False


# パス
SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "escomi_crawler.db"
ERROR_LOG_PATH = SCRIPT_DIR / "scraping_errors.log"

# 名前の最小有効文字数（これ以下は無効として除外）
NAME_MIN_LENGTH = 2

# リクエストタイムアウト（秒）
REQUEST_TIMEOUT = 15

# 1店舗処理後の待機時間（秒）: 1.5〜3.5秒のランダムでIP分散
SLEEP_BETWEEN_SHOPS_MIN = 1.5
SLEEP_BETWEEN_SHOPS_MAX = 3.5

# Gemini API 呼び出し後の待機時間（RPM制限回避）
SLEEP_AFTER_GEMINI = 12

# Gemini に渡す HTML の最大文字数
HTML_SNIPPET_MAX = 12000

# セレクタ学習用プロンプト
SELECTOR_DISCOVERY_PROMPT = """あなたはHTML解析の専門家です。以下のHTMLから「本日の出勤キャスト」の一覧を抽出するためのCSSセレクタを特定してください。

抽出したい項目:
1. cast_list: 各キャストを囲む親要素（1キャスト1要素になるリストの各アイテム）
2. name: キャスト名（cast_list の子要素として指定）
3. time: 出勤時間（例: 12:00-LAST）
4. status: 空き状況（例: すぐご案内可、予約満了）

【出力形式】以下のJSON形式のみで返してください。説明は不要です。
{
  "cast_list": "親要素のCSSセレクタ",
  "name": "名前のCSSセレクタ（親要素内）",
  "time": "時間のCSSセレクタ（親要素内）",
  "status": "空き状況のCSSセレクタ（親要素内）"
}

セレクタが見つからない項目は空文字 "" にしてください。cast_list は必須です。"""


def _get_domain(url: str) -> str:
    """URL からドメインを取得"""
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc or ""
        return netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _cleanse_name(name: str) -> str:
    """
    キャスト名のクレンジング。高速運用フェーズで必ず適用。
    - カッコとその中身: 田中(たなか)、ゆな（24）等を除去
    - 特殊記号・絵文字: ◆■✨✅🌟等を除去
    - 空白: 前後の全角・半角スペースをトリミング
    - 2文字未満は無効として空文字を返す
    """
    if not name or not isinstance(name, str):
        return ""
    s = name
    # カッコ内を除去（半角・全角）
    s = re.sub(r"\s*\([^)]*\)\s*", "", s)
    s = re.sub(r"\s*（[^）]*）\s*", "", s)
    # 絵文字・特殊記号を除去（Unicode 範囲で除去）
    s = re.sub(r"[\u2600-\u27BF]", "", s)   # 絵文字・記号
    s = re.sub(r"[\u2B50\u2705\u274C\u2B55\u25A0-\u25FF]", "", s)  # ★✅◆■等
    s = re.sub(r"[◆■●○★☆♪♫✓✔✗✘]", "", s)
    s = re.sub(r"[\U0001F300-\U0001F9FF]", "", s)  # 絵文字ブロック
    # 前後の空白（全角・半角）をトリミング
    s = re.sub(r"^[\s\u3000]+|[\s\u3000]+$", "", s)
    s = s.strip()
    # 2文字未満は無効
    if len(s) < NAME_MIN_LENGTH:
        return ""
    return s


def _cleanse_therapists(therapists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """therapists 配列の name をクレンジングし、無効なデータを除外"""
    result = []
    for t in therapists:
        cleaned = {**t, "name": _cleanse_name(t.get("name", ""))}
        if cleaned.get("name") and len(cleaned["name"]) >= NAME_MIN_LENGTH:
            result.append(cleaned)
    return result


def _log_scraping_error(shop_id: int, shop_name: str, url: str, reason: str) -> None:
    """
    学習失敗店舗を scraping_errors.log に詳細追記。
    形式: [日時] [ShopID] [店舗名] [URL] [エラー理由（AI学習失敗 / データ取得0件）]
    """
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [ShopID:{shop_id}] [{shop_name}] [{url}] [{reason}]\n"
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def init_db() -> None:
    """scraping_rules と therapist_logs テーブルを作成"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scraping_rules (
                shop_id INTEGER PRIMARY KEY,
                domain TEXT NOT NULL,
                selector_json TEXT NOT NULL,
                last_verified DATETIME NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scraping_rules_domain ON scraping_rules(domain)")
        # therapist_logs: shop_id + name の組み合わせで出勤履歴を管理（別店舗の同名キャストを別人としてレア判定）
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


def get_rules_for_shop(shop_id: int) -> Optional[Dict[str, str]]:
    """shop_id のセレクタルールを取得"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT selector_json FROM scraping_rules WHERE shop_id = ?",
            (shop_id,),
        )
        row = cur.fetchone()
        if row:
            try:
                return json.loads(row["selector_json"])
            except (json.JSONDecodeError, TypeError):
                pass
    return None


def get_rules_for_domain(domain: str) -> Optional[Dict[str, str]]:
    """同一ドメインのセレクタルールを取得（他店舗で学習済みの場合）"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT selector_json FROM scraping_rules WHERE domain = ? LIMIT 1",
            (domain,),
        )
        row = cur.fetchone()
        if row:
            try:
                return json.loads(row["selector_json"])
            except (json.JSONDecodeError, TypeError):
                pass
    return None


def save_therapist_log(shop_id: int, name: str) -> None:
    """therapist_logs に出勤記録を保存（shop_id + name の組み合わせで管理、同日同名は重複しない）"""
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
    """過去30日間の出勤回数を返す。3回以下ならレアとみなす（shop_id + name で別人判定）"""
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


def apply_rare_tags(shop_id: int, therapists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """過去30日間の出勤が3回以下のキャストに「レア」タグを追加（shop_id 単位で別人判定）"""
    result = []
    for t in therapists:
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


def save_rules(shop_id: int, domain: str, rules: Dict[str, str]) -> None:
    """セレクタルールを保存"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO scraping_rules (shop_id, domain, selector_json, last_verified)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(shop_id) DO UPDATE SET
                selector_json = excluded.selector_json,
                last_verified = excluded.last_verified
            """,
            (shop_id, domain, json.dumps(rules, ensure_ascii=False), now),
        )
        conn.commit()


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

    if missing:
        print(f"ERROR: 以下の環境変数が未設定です: {', '.join(missing)}")
        sys.exit(1)

    return {
        "site_url": site_url,
        "user": user,
        "app_password": app_password,
        "gemini_key": gemini_key or "",
    }


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


def _extract_html_snippet(html: str) -> str:
    """出勤表周りのHTMLを抽出（Gemini用にサイズ制限）"""
    try:
        soup = BeautifulSoup(html, "html.parser")
        # script, style を除去
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = str(soup.body) if soup.body else str(soup)
        return text[:HTML_SNIPPET_MAX]
    except Exception:
        return html[:HTML_SNIPPET_MAX]


def discover_selectors_with_gemini(html: str, gemini_key: str) -> Optional[Dict[str, str]]:
    """Gemini でセレクタを自動特定"""
    if not HAS_GEMINI or not gemini_key:
        return None

    snippet = _extract_html_snippet(html)
    if len(snippet) < 200:
        return None

    try:
        client = genai.Client(api_key=gemini_key)
        prompt = f"{SELECTOR_DISCOVERY_PROMPT}\n\n【HTML】\n{snippet}"
        response = client.models.generate_content(
            model="models/gemini-1.5-flash",  # トークン消費最小化
            contents=prompt,
        )
        raw = (response.text or "").strip()
        if not raw:
            return None

        # ```json ... ``` を除去
        if "```" in raw:
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
            raw = m.group(1).strip() if m else raw

        data = json.loads(raw)
        rules = {
            "cast_list": (data.get("cast_list") or "").strip(),
            "name": (data.get("name") or "").strip(),
            "time": (data.get("time") or "").strip(),
            "status": (data.get("status") or "").strip(),
        }
        if rules["cast_list"]:
            return rules
    except Exception as e:
        print(f"    [Gemini] セレクタ特定失敗: {type(e).__name__}: {e}")
    return None


def scrape_schedule(html: str, rules: Dict[str, str]) -> Tuple[List[Dict[str, Any]], str]:
    """
    BeautifulSoup で HTML から出勤スケジュールを抽出。
    戻り値: (therapists配列, shop_availability文字列)
    """
    therapists: List[Dict[str, Any]] = []
    availability = ""

    try:
        soup = BeautifulSoup(html, "html.parser")
        cast_list_sel = rules.get("cast_list", "")
        name_sel = rules.get("name", "")
        time_sel = rules.get("time", "")
        status_sel = rules.get("status", "")

        if not cast_list_sel:
            return ([], "")

        items = soup.select(cast_list_sel)
        status_texts: List[str] = []

        for item in items:
            name_el = item.select_one(name_sel) if name_sel else None
            time_el = item.select_one(time_sel) if time_sel else None
            status_el = item.select_one(status_sel) if status_sel else None

            name = (name_el.get_text(strip=True) if name_el else "").strip()
            time_val = (time_el.get_text(strip=True) if time_el else "").strip()
            status_val = (status_el.get_text(strip=True) if status_el else "").strip()

            if not name:
                continue

            if status_val:
                status_texts.append(status_val)

            therapists.append({
                "name": name,
                "time": time_val or "",
                "tags": [],
                "status": status_val or "",
            })

        for s in status_texts:
            if "すぐ" in s or "空き" in s or "案内" in s:
                availability = s
                break
        if not availability and status_texts:
            availability = status_texts[0]

        return (therapists, availability)
    except Exception:
        return ([], "")


def _build_rest_urls(site_url: str) -> List[str]:
    """REST API の URL 候補を返す"""
    base = site_url.rstrip("/")
    return [
        f"{base}/wp-json/ai-engine/v1/update",
        f"{base}/?rest_route=/ai-engine/v1/update",
    ]


def update_schedule_only(
    site_url: str,
    user: str,
    app_password: str,
    post_id: int,
    therapists: List[Dict[str, Any]],
    availability: str,
) -> bool:
    """
    WordPress REST API で shop_today_therapists と shop_availability を完全上書き更新。
    ※ update_post_meta により既存値は置換され、履歴蓄積によるDB肥大化は発生しない。
    """
    meta = {
        "shop_today_therapists": therapists,
        "shop_availability": (availability or "").strip(),
    }

    payload = {
        "shop_post_id": post_id,
        "meta": meta,
        "summary": "",
        "log_type": "hourly",
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


def process_shop(config: Dict[str, str], shop: Dict, index: int) -> bool:
    """
    1店舗のスクレイピング・更新を実行。
    DBのセレクタを優先、なければAIで学習。
    """
    post_id = int(shop["post_id"])
    name = shop["name"]
    url = shop["official_url"]
    domain = _get_domain(url)

    print(f"\n[{index}] {name} (post_id: {post_id})")
    print(f"    URL: {url}")

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; EscomiScheduleBot/1.0)"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        html = resp.text
    except requests.RequestException as e:
        print(f"    ERROR: リクエスト失敗: {type(e).__name__}: {e}")
        return True

    # 1. セレクタ取得: shop_id → domain の順でDB参照（高速運用フェーズ用）
    rules = get_rules_for_shop(post_id)
    if not rules:
        rules = get_rules_for_domain(domain)
        if rules:
            save_rules(post_id, domain, rules)  # 同一ドメインのルールを自店舗にも保存

    # 2. 学習フェーズ: セレクタがない場合のみ Gemini 発動
    if not rules or not rules.get("cast_list"):
        if HAS_GEMINI and config.get("gemini_key"):
            print(f"    [AI学習] セレクタを自動特定中...")
            rules = discover_selectors_with_gemini(html, config["gemini_key"])
            time.sleep(SLEEP_AFTER_GEMINI)
            if rules:
                save_rules(post_id, domain, rules)
                print(f"    [AI学習] セレクタを保存しました")
            else:
                print(f"    [AI学習] セレクタの特定に失敗（スキップ）")
                _log_scraping_error(post_id, name, url, "AI学習失敗")
                time.sleep(SLEEP_AFTER_GEMINI)
                return True
        else:
            print(f"    スキップ: セレクタ未登録（GEMINI_API_KEY で学習可能）")
            _log_scraping_error(post_id, name, url, "AI学習失敗")
            return True

    # 3. BeautifulSoup で抽出（AI非稼働）
    therapists, availability = scrape_schedule(html, rules)

    # 4. 学習フェーズ: 一人も取得できなかった場合のみ Gemini 再学習
    if not therapists and HAS_GEMINI and config.get("gemini_key"):
        print(f"    [AI学習] 抽出0件のため再学習を試行...")
        new_rules = discover_selectors_with_gemini(html, config["gemini_key"])
        time.sleep(SLEEP_AFTER_GEMINI)
        if new_rules and new_rules.get("cast_list"):
            rules = new_rules
            save_rules(post_id, domain, rules)
            therapists, availability = scrape_schedule(html, rules)
            if therapists:
                print(f"    [AI学習] 新セレクタで抽出成功")

    # 5. データクレンジング（(たなか)(42)絵文字等を必ず除去）
    therapists = _cleanse_therapists(therapists)

    # 6. therapist_logs に保存し、shop_id+name でレア判定（別店舗の同名は別人）
    for t in therapists:
        n = (t.get("name") or "").strip()
        if n:
            save_therapist_log(post_id, n)
    therapists = apply_rare_tags(post_id, therapists)

    if not therapists:
        print("    抽出結果: 0件（スキップ）")
        _log_scraping_error(post_id, name, url, "データ取得0件")
        return True

    print(f"    抽出: {len(therapists)} 名")
    if availability:
        print(f"    空き状況: {availability}")

    success = update_schedule_only(
        config["site_url"],
        config["user"],
        config["app_password"],
        post_id,
        therapists,
        availability,
    )

    if success:
        print("    ✓ 更新完了")
    else:
        print("    ERROR: WordPress への保存に失敗")

    return True


def main() -> None:
    config = get_config()
    init_db()

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

    if not valid_shops:
        print("ERROR: URL が有効な店舗が 1 件もありません。")
        sys.exit(1)

    print(f"全店舗: {len(valid_shops)} 件")

    for i, shop in enumerate(valid_shops, 1):
        process_shop(config, shop, i)
        if i < len(valid_shops):
            delay = random.uniform(SLEEP_BETWEEN_SHOPS_MIN, SLEEP_BETWEEN_SHOPS_MAX)
            time.sleep(delay)

    print(f"\n--- 完了（{len(valid_shops)} 件処理）---")


if __name__ == "__main__":
    main()
