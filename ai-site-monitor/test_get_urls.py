#!/usr/bin/env python3
"""
test_get_urls.py
WordPress REST API から shop カスタム投稿のリストを取得し、
最初の5件の店名と公式サイトURLを表示するテストスクリプト。

【認証】Application Passwords（Basic認証）
【設定】.env または環境変数で WP_SITE_URL, WP_USER, WP_APP_PASSWORD を指定
"""

import os
import sys
from base64 import b64encode
from typing import Dict, List

# .env があれば読み込み（python-dotenv）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests


def get_config():
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
            "  .env ファイルを作成するか、環境変数を設定してください。\n"
            "  例: export WP_SITE_URL=https://example.com"
        )
        sys.exit(1)

    return {"site_url": site_url, "user": user, "app_password": app_password}


def fetch_shops(site_url: str, user: str, app_password: str) -> List[Dict]:
    """REST API で shop 投稿一覧を取得"""
    url = f"{site_url}/wp-json/wp/v2/shop"
    auth_str = f"{user}:{app_password}"
    auth_b64 = b64encode(auth_str.encode()).decode()
    headers = {"Authorization": f"Basic {auth_b64}"}

    # _fields を指定すると custom field が返らない場合があるため、必要最小限のみ取得
    params = {"per_page": 100, "_fields": "id,title,official_url,acf"}

    all_shops = []
    page = 1

    while True:
        params["page"] = page
        resp = requests.get(url, headers=headers, params=params, timeout=30)

        if resp.status_code != 200:
            print(f"ERROR: API エラー (HTTP {resp.status_code})")
            print(resp.text[:500] if resp.text else "(レスポンスなし)")
            sys.exit(1)

        data = resp.json()
        if not data:
            break

        all_shops.extend(data)
        if len(data) < params["per_page"]:
            break
        page += 1

    return all_shops


def main():
    config = get_config()
    shops = fetch_shops(
        config["site_url"],
        config["user"],
        config["app_password"],
    )

    print(f"取得件数: {len(shops)} 件\n")
    print("--- 最初の5件 ---\n")

    for i, shop in enumerate(shops[:5], 1):
        post_id = shop.get("id", "")
        title = shop.get("title", {})
        name = title.get("rendered", "（タイトルなし）") if isinstance(title, dict) else str(title)
        # official_url: トップレベル or acf 内（ACFのREST API有効時）
        official_url = (
            shop.get("official_url")
            or (shop.get("acf") or {}).get("official_url")
            or ""
        )
        official_url = official_url if official_url else "（未設定）"

        print(f"{i}. 店名: {name}")
        print(f"   post_id: {post_id}")
        print(f"   公式サイトURL: {official_url}")
        print()

    print("--- 完了 ---")


if __name__ == "__main__":
    main()
