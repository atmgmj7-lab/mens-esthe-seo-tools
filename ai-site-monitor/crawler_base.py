#!/usr/bin/env python3
"""
crawler_base.py
WordPress REST API から店舗リストを取得し、Playwright で公式サイトを巡回する AI クローラーのベーススクリプト。

【テスト仕様】URL が有効な店舗のうち、最初の 3 件のみ巡回
【出力】各ページの <title> を表示、screenshots/ にスクリーンショットを保存
"""

import asyncio
import os
import re
import sys
from base64 import b64encode
from pathlib import Path
from typing import Dict, List, Optional

# .env があれば読み込み
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests
from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PlaywrightTimeoutError


# テスト用：巡回する店舗数
CRAWL_LIMIT = 3

# スクリーンショット保存先
SCREENSHOTS_DIR = Path(__file__).resolve().parent / "screenshots"

# タイムアウト（ミリ秒）
PAGE_TIMEOUT_MS = 30000


def get_config() -> Dict[str, str]:
    """環境変数または .env から設定を取得"""
    site_url = os.environ.get("WP_SITE_URL", "").rstrip("/")
    user = os.environ.get("WP_USER", "")
    app_password = os.environ.get("WP_APP_PASSWORD", "")

    if not site_url or not user or not app_password:
        missing = []
        if not site_url:
            missing.append("WP_SITE_URL")
        if not user:
            missing.append("WP_USER")
        if not app_password:
            missing.append("WP_APP_PASSWORD")
        print(
            f"ERROR: 以下の環境変数が未設定です: {', '.join(missing)}\n"
            "  .env ファイルを作成するか、環境変数を設定してください。"
        )
        sys.exit(1)

    return {"site_url": site_url, "user": user, "app_password": app_password}


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
    """shop データから post_id, name, official_url を抽出。URL が無効なら None"""
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


def sanitize_filename(name: str) -> str:
    """ファイル名に使えない文字を除去・置換"""
    # Windows / macOS で使えない文字
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", "_", name)
    name = name.strip("._")[:100]  # 長さ制限
    return name or "unknown"


async def crawl_shop(
    browser: Browser,
    post_id: str,
    name: str,
    url: str,
    index: int,
) -> None:
    """1店舗の公式サイトにアクセスし、title 取得・スクリーンショット保存"""
    print(f"\n[{index}] {name} (post_id: {post_id})")
    print(f"    URL: {url}")

    try:
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()

        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

        title = await page.title()
        print(f"    <title>: {title}")

        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = sanitize_filename(name)
        filename = f"{post_id}_{safe_name}.png"
        filepath = SCREENSHOTS_DIR / filename

        await page.screenshot(path=str(filepath), full_page=False)
        print(f"    スクリーンショット保存: {filepath}")

        await context.close()

    except PlaywrightTimeoutError:
        print(f"    ERROR: タイムアウト ({PAGE_TIMEOUT_MS / 1000}秒)")
    except Exception as e:
        print(f"    ERROR: {type(e).__name__}: {e}")


async def main_async() -> None:
    config = get_config()
    shops = fetch_shops(
        config["site_url"],
        config["user"],
        config["app_password"],
    )

    # URL が有効な店舗のみ抽出し、最初の CRAWL_LIMIT 件
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

    print(f"巡回対象: {len(valid_shops)} 件")
    print(f"スクリーンショット保存先: {SCREENSHOTS_DIR}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for i, shop in enumerate(valid_shops, 1):
            await crawl_shop(
                browser,
                str(shop["post_id"]),
                shop["name"],
                shop["official_url"],
                i,
            )

        await browser.close()

    print("\n--- 完了 ---")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
