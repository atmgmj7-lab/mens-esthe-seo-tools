import json
import os
from pathlib import Path

_BASE = Path(__file__).resolve().parent
# 入力ファイルと出力ファイルを指定
INPUT_FILE = _BASE / "refle_full_raw_840.json"
OUTPUT_FILE = _BASE / "refle_master_clean.json"

def deduplicate():
    if not INPUT_FILE.exists():
        print(f"❌ 入力ファイルが見つかりません: {INPUT_FILE}")
        return

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"📊 処理前: {len(data)} 件")

    unique_shops = {}
    for shop in data:
        name = shop.get("shop_name", "不明")
        phone = shop.get("phone", "不明")
        key = (name, phone)
        
        if key not in unique_shops:
            unique_shops[key] = shop
        else:
            # URLがある方を優先して残す
            if not unique_shops[key].get("official_url") and shop.get("official_url"):
                unique_shops[key] = shop

    clean_data = list(unique_shops.values())
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(clean_data, f, indent=2, ensure_ascii=False)

    print(f"✨ 処理後: {len(clean_data)} 件")
    print(f"📁 保存完了: {OUTPUT_FILE.name}")

if __name__ == "__main__":
    deduplicate()