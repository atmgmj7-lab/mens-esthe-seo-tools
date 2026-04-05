#!/usr/bin/env python3
"""
refle_master.json を読み込み、WordPress REST API 経由で一括インポート

- status: "new" → 新規投稿（shop）を作成
- status: "existing" → 既存記事を更新
- 店舗画像をダウンロードしてアイキャッチ（featured_media）に設定
"""

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

_BASE = Path(__file__).resolve().parent
load_dotenv(_BASE / ".env")

# ============================================================
# 設定
# ============================================================
REFLE_MASTER = _BASE / "refle_master.json"
REQUEST_DELAY = 1.0
USER_AGENT = "Mozilla/5.0 (compatible; BulkImportWP/1.0)"


def get_wp_base() -> str:
    """WordPress ベースURL"""
    wp = os.environ.get("WP_BASE_URL", "")
    parsed = urlparse(wp)
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def get_auth() -> tuple[str, str]:
    """Basic認証"""
    user = os.environ.get("WP_USER", "")
    pw = os.environ.get("WP_APP_PASSWORD", "")
    if not user or not pw:
        raise ValueError("WP_USER と WP_APP_PASSWORD を .env に設定してください")
    return (user, pw)


def download_image(url: str) -> tuple[bytes | None, str]:
    """画像をダウンロードし (bytes, filename) を返す"""
    if not url or not url.startswith("http"):
        return None, ""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        resp.raise_for_status()
        data = resp.content
        if len(data) < 100:
            return None, ""
        # 拡張子を推測
        ct = resp.headers.get("Content-Type", "")
        ext = ".jpg"
        if "png" in ct:
            ext = ".png"
        elif "gif" in ct:
            ext = ".gif"
        elif "webp" in ct:
            ext = ".webp"
        else:
            m = re.search(r"\.(jpe?g|png|gif|webp)", url, re.I)
            if m:
                ext = "." + m.group(1).lower()
        return data, f"shop{ext}"
    except Exception as e:
        print(f"      [画像DL失敗] {url}: {e}")
        return None, ""


def upload_media(base: str, auth: tuple, image_data: bytes, filename: str, alt: str = "") -> int | None:
    """WordPress にメディアをアップロードし、attachment ID を返す"""
    url = f"{base}/wp-json/wp/v2/media"
    mime = "image/jpeg"
    if ".png" in filename.lower():
        mime = "image/png"
    elif ".gif" in filename.lower():
        mime = "image/gif"
    elif ".webp" in filename.lower():
        mime = "image/webp"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": mime,
        "User-Agent": USER_AGENT,
    }
    try:
        resp = requests.post(url, data=image_data, headers=headers, auth=auth, timeout=30)
        if resp.status_code not in (200, 201):
            print(f"      [メディアアップロード失敗] {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
        mid = data.get("id")
        if mid and alt:
            requests.post(f"{base}/wp-json/wp/v2/media/{mid}", json={"alt_text": alt}, auth=auth, timeout=10)
        return int(mid) if mid else None
    except Exception as e:
        print(f"      [メディアアップロードエラー] {e}")
        return None


def create_shop(base: str, auth: tuple, shop: dict, featured_media_id: int | None) -> int | None:
    """新規店舗投稿を作成"""
    url = f"{base}/wp-json/wp/v2/shop"
    payload = {
        "title": shop.get("name", "未設定"),
        "content": "",
        "status": "draft",
        "meta": {
            "shop_address": shop.get("address", ""),
            "shop_tel": shop.get("phone", ""),
            "shop_hours": shop.get("shop_hours", ""),
            "official_url": shop.get("official_url", ""),
        },
    }
    if featured_media_id:
        payload["featured_media"] = featured_media_id
    try:
        resp = requests.post(url, json=payload, auth=auth, timeout=30)
        if resp.status_code in (200, 201):
            return resp.json().get("id")
        print(f"      [作成失敗] {resp.status_code}: {resp.text[:200]}")
        return None
    except Exception as e:
        print(f"      [作成エラー] {e}")
        return None


def update_shop(base: str, auth: tuple, post_id: int, shop: dict, featured_media_id: int | None) -> bool:
    """既存店舗投稿を更新"""
    url = f"{base}/wp-json/wp/v2/shop/{post_id}"
    payload = {
        "meta": {
            "shop_address": shop.get("address", ""),
            "shop_tel": shop.get("phone", ""),
            "shop_hours": shop.get("shop_hours", ""),
            "official_url": shop.get("official_url", ""),
        },
    }
    if featured_media_id:
        payload["featured_media"] = featured_media_id
    try:
        resp = requests.put(url, json=payload, auth=auth, timeout=30)
        if resp.status_code == 200:
            return True
        print(f"      [更新失敗] {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"      [更新エラー] {e}")
        return False


def main():
    if not REFLE_MASTER.exists():
        print(f"refle_master.json が見つかりません: {REFLE_MASTER}")
        return 1

    with open(REFLE_MASTER, encoding="utf-8") as f:
        master = json.load(f)

    base = get_wp_base()
    auth = get_auth()

    new_count = 0
    update_count = 0
    skip_count = 0

    for i, shop in enumerate(master):
        name = shop.get("name", "")
        status = shop.get("status", "")
        post_id = shop.get("shop_post_id")

        if status == "existing" and not post_id:
            skip_count += 1
            continue

        print(f"[{i+1}/{len(master)}] {name[:30]}... ({status})")

        # 画像ダウンロード → アップロード
        img_url = shop.get("image_url", "")
        featured_id = None
        if img_url:
            data, fname = download_image(img_url)
            if data and fname:
                featured_id = upload_media(base, auth, data, fname, alt=name)
                time.sleep(0.5)

        time.sleep(REQUEST_DELAY)

        if status == "new":
            new_id = create_shop(base, auth, shop, featured_id)
            if new_id:
                new_count += 1
                print(f"      → 新規作成 ID:{new_id}")
            else:
                skip_count += 1
        else:
            if update_shop(base, auth, post_id, shop, featured_id):
                update_count += 1
                print(f"      → 更新完了 ID:{post_id}")
            else:
                skip_count += 1

    print(f"\n完了: 新規 {new_count} / 更新 {update_count} / スキップ {skip_count}")
    return 0


if __name__ == "__main__":
    exit(main())
