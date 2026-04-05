#!/usr/bin/env python3
"""
refle_full_raw_840.json の重複店舗を削除し refle_master_clean.json に保存
"""
import json
from pathlib import Path

_BASE = Path(__file__).resolve().parent
INPUT_FILE = _BASE / "refle_full_raw_840.json"
OUTPUT_FILE = _BASE / "refle_master_clean.json"


def deduplicate():
    if not INPUT_FILE.exists():
        print(f"❌ ファイルが見つかりません: {INPUT_FILE}")
        return

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"📊 処理前: {len(data)} 件")

    unique_shops = {}

    for shop in data:
        key = (shop.get("shop_name"), shop.get("phone"))

        if key not in unique_shops:
            unique_shops[key] = shop
        else:
            if not unique_shops[key].get("official_url") and shop.get("official_url"):
                unique_shops[key] = shop

    clean_data = list(unique_shops.values())

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(clean_data, f, indent=2, ensure_ascii=False)

    print(f"✨ 処理後: {len(clean_data)} 件 (削除された重複: {len(data) - len(clean_data)} 件)")
    print(f"📁 保存先: {OUTPUT_FILE}")


if __name__ == "__main__":
    deduplicate()
