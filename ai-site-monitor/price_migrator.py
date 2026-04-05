#!/usr/bin/env python3
"""
price_migrator.py
リフナビ等からスクレイピングして escomi_crawler.db に保存された店舗データを利用し、
全店舗の「60分料金」を一括で WordPress の shop_price_60min ACF に登録するワンタイムスクリプト。

【データソース】
1. escomi_crawler.db の shops テーブル（post_id, price/price_text/basic_price）
2. または --json で指定した JSON ファイル（shop_post_id, price/price_text/basic_price）

【実行例】
  python price_migrator.py
  python price_migrator.py --json path/to/shops_with_price.json
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from base64 import b64encode
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

import requests

import os
# --- ここに直接合鍵を書き込みます ---
os.environ["WP_SITE_URL"] = "https://mens-esthe-kuchikomi.com/"
os.environ["WP_USER"] = "master"
os.environ["WP_APP_PASSWORD"] = "VUXv iM0G PmC9 MJpk Ommb ggzm"
# ----------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "escomi_crawler.db"

# 料金抽出用正規表現（60分 or 最安値の基本料金、5,000〜50,000円）
PRICE_PATTERNS = [
    r"60\s*分[^\d]*(?:¥|円)?\s*[\d,]*(\d{4,5})",
    r"60分[^\d]*(?:¥|円)?\s*[\d,]*(\d{4,5})",
    r"60min[^\d]*(?:¥|円)?\s*[\d,]*(\d{4,5})",
    r"(?:¥|円)?\s*[\d,]*(\d{4,5})\s*(?:円)?[^\d]*60\s*分",
    r"基本[^\d]*(?:¥|円)?\s*[\d,]*(\d{4,5})",
    r"最安[^\d]*(?:¥|円)?\s*[\d,]*(\d{4,5})",
    r"(\d{4,5})\s*円",  # フォールバック: 4〜5桁の数字+円
]
PRICE_MIN, PRICE_MAX = 5000, 50000


def get_config() -> Dict[str, str]:
    """環境変数から設定を取得"""
    site_url = os.environ.get("WP_SITE_URL", "").rstrip("/")
    user = os.environ.get("WP_USER", "")
    app_password = os.environ.get("WP_APP_PASSWORD", "")

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

    return {"site_url": site_url, "user": user, "app_password": app_password}


def inspect_db_schema(db_path: Path) -> None:
    """DB のスキーマを表示"""
    if not db_path.exists():
        print(f"DB が存在しません: {db_path}")
        return

    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [r[0] for r in cur.fetchall()]

    print("\n=== escomi_crawler.db スキーマ ===")
    for t in tables:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(f"PRAGMA table_info({t})")
            cols = cur.fetchall()
        col_str = ", ".join(f"{c[1]}({c[2]})" for c in cols)
        print(f"  {t}: {col_str}")
    print()


def load_from_db(db_path: Path) -> List[Dict[str, Any]]:
    """
    DB から店舗データを読み込み。
    shops テーブルがあれば使用。なければ空リスト。
    """
    if not db_path.exists():
        return []

    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='shops'"
        )
        if not cur.fetchone():
            return []

        cur = conn.execute("PRAGMA table_info(shops)")
        cols = [c[1] for c in cur.fetchall()]

        # post_id / shop_id と price 系カラムを探す
        id_col = "post_id" if "post_id" in cols else ("shop_id" if "shop_id" in cols else None)
        price_cols = [c for c in cols if c in ("price", "price_text", "basic_price", "price_60")]
        name_col = "shop_name" if "shop_name" in cols else ("name" if "name" in cols else None)

        if not id_col:
            print("WARN: shops テーブルに post_id または shop_id がありません")
            return []

        select_cols = [id_col]
        if name_col:
            select_cols.append(name_col)
        for pc in price_cols:
            if pc not in select_cols:
                select_cols.append(pc)

        cur = conn.execute(f"SELECT {', '.join(select_cols)} FROM shops")
        rows = cur.fetchall()

    result = []
    for row in rows:
        d = dict(zip(select_cols, row))
        d["_id_col"] = id_col
        d["_name_col"] = name_col
        result.append(d)
    return result


def load_from_json(path: Path) -> List[Dict[str, Any]]:
    """JSON ファイルから店舗データを読み込み（shop_post_id/post_id と price 系必須）"""
    if not path.exists():
        print(f"ERROR: ファイルが存在しません: {path}")
        return []

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        data = [data]

    result = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        post_id = item.get("shop_post_id") or item.get("post_id") or item.get("id")
        if post_id is None:
            continue
        name = item.get("shop_name") or item.get("name") or f"店舗{i+1}"
        price_raw = (
            item.get("price")
            or item.get("price_text")
            or item.get("basic_price")
            or item.get("price_60")
            or item.get("price_textarea")
            or ""
        )
        result.append({
            "post_id": int(post_id),
            "shop_name": str(name),
            "price_raw": str(price_raw) if price_raw else "",
        })
    return result


def extract_price_60(text: str) -> Optional[int]:
    """
    料金テキストから「60分の料金」または「最安値の基本料金」を抽出。
    例: "60分 12,000円" → 12000
    """
    if not text or not isinstance(text, str):
        return None

    text = text.replace(" ", "").replace("　", "")
    candidates: List[int] = []

    for pattern in PRICE_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            try:
                raw = m.group(1).replace(",", "")
                val = int(raw)
                if PRICE_MIN <= val <= PRICE_MAX:
                    candidates.append(val)
            except (ValueError, IndexError):
                pass

    return min(candidates) if candidates else None


def update_shop_price_60min(
    site_url: str,
    user: str,
    app_password: str,
    post_id: int,
    price_60: int,
) -> bool:
    """WordPress REST API で shop_price_60min を更新"""
    url = f"{site_url.rstrip('/')}/wp-json/ai-engine/v1/update"
    alt_url = f"{site_url.rstrip('/')}/?rest_route=/ai-engine/v1/update"

    payload = {
        "shop_post_id": post_id,
        "meta": {"shop_price_60min": price_60},
        "summary": "",
        "log_type": "price_migrate",
    }
    auth = (user, app_password)

    for u in (url, alt_url):
        try:
            resp = requests.post(u, json=payload, auth=auth, timeout=30)
            if resp.status_code in (200, 201):
                return True
        except Exception:
            continue
    return False


def create_shops_table_if_missing(db_path: Path) -> None:
    """shops テーブルがなければ作成（post_id, shop_name, price_text 等）"""
    if not db_path.exists():
        return
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shops (
                post_id INTEGER PRIMARY KEY,
                shop_name TEXT,
                price_text TEXT,
                price INTEGER,
                basic_price TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="60分料金を WordPress shop_price_60min に一括登録")
    parser.add_argument("--json", type=str, help="JSON ファイルパス（DB の代わりに使用）")
    parser.add_argument("--dry-run", action="store_true", help="実際には送信せず、抽出結果のみ表示")
    parser.add_argument("--init-db", action="store_true", help="shops テーブルを自動作成（未作成時）")
    args = parser.parse_args()

    # 1. スキーマ確認
    if args.init_db:
        create_shops_table_if_missing(DB_PATH)
    inspect_db_schema(DB_PATH)

    # 2. データ読み込み
    if args.json:
        data_path = Path(args.json)
        if not data_path.is_absolute():
            data_path = SCRIPT_DIR / data_path
        rows = load_from_json(data_path)
        print(f"JSON から {len(rows)} 件読み込み: {data_path}")
    else:
        rows = load_from_db(DB_PATH)
        if not rows:
            print("shops テーブルが存在しないか、データがありません。")
            print("  --json path/to/file.json で JSON ファイルを指定してください。")
            print("  JSON 形式: [{\"shop_post_id\": 123, \"price\": \"60分 12,000円\"}, ...]")
            sys.exit(1)
        print(f"DB から {len(rows)} 件読み込み")

    # 3. データ正規化と料金抽出
    items: List[Dict[str, Any]] = []
    for r in rows:
        post_id = r.get("post_id") or r.get("shop_id")
        if post_id is None:
            continue

        name = (
            r.get("shop_name")
            or r.get("name")
            or r.get("title")
            or f"post_id:{post_id}"
        )

        price_raw = ""
        price_60: Optional[int] = None
        for key in ("price", "price_text", "basic_price", "price_60", "price_raw", "price_textarea"):
            val = r.get(key)
            if val is None:
                continue
            if isinstance(val, int) and PRICE_MIN <= val <= PRICE_MAX:
                price_60 = val
                break
            price_raw = str(val).strip()
            if price_raw:
                break

        if price_60 is None and price_raw:
            price_60 = extract_price_60(price_raw)

        items.append({
            "post_id": int(post_id),
            "name": str(name),
            "price_60": price_60,
            "price_raw": price_raw[:80] if price_raw else "",
        })

    # 4. 抽出失敗のログ
    skipped = [x for x in items if x["price_60"] is None]
    to_update = [x for x in items if x["price_60"] is not None]

    if skipped:
        print("\n--- 料金抽出できずスキップ ---")
        for s in skipped:
            print(f"  [{s['post_id']}] {s['name']} | 元データ: {s['price_raw'] or '(空)'}")

    # 5. WordPress へ送信
    config = get_config()
    success_count = 0

    iter_items = tqdm(to_update, desc="更新中") if HAS_TQDM else to_update
    for item in iter_items:
        if not HAS_TQDM:
            print(f"  処理中: [{item['post_id']}] {item['name']} → {item['price_60']}円")

        if args.dry_run:
            success_count += 1
            continue

        ok = update_shop_price_60min(
            config["site_url"],
            config["user"],
            config["app_password"],
            item["post_id"],
            item["price_60"],
        )
        if ok:
            success_count += 1

    # 6. サマリー
    total = len(items)
    print(f"\n=== 完了 ===")
    print(f"全 {total} 店舗中、{success_count} 店舗の料金更新に成功")
    if skipped:
        print(f"（{len(skipped)} 店舗は料金抽出失敗のためスキップ）")
    if args.dry_run:
        print("（--dry-run のため実際の送信は行っていません）")


if __name__ == "__main__":
    main()
