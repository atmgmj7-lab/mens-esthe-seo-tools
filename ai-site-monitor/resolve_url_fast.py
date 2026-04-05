#!/usr/bin/env python3
"""
refle_full_list.json 内の全店舗の /pUrl/ 転送リンクを
requests で解決し、refle_final_monitor_list.json に保存する
"""
import json
import time
from pathlib import Path

import requests

_BASE = Path(__file__).resolve().parent
INPUT_FILE = _BASE / "refle_full_raw_840.json"
OUTPUT_FILE = _BASE / "refle_final_monitor_list.json"
REQUEST_DELAY = 0.5


def resolve_url(url: str) -> str | None:
    """requests でリダイレクト先を取得（ブラウザ不要）"""
    try:
        resp = requests.head(url, allow_redirects=True, timeout=10)
        if resp.status_code == 405:
            resp = requests.get(url, allow_redirects=True, timeout=10, stream=True)
        return resp.url if resp else None
    except Exception:
        try:
            resp = requests.get(url, allow_redirects=True, timeout=10, stream=True)
            return resp.url if resp else None
        except Exception:
            return None


def main():
    input_file = INPUT_FILE
    if not input_file.exists():
        fallback = _BASE / "refle_full_list.json"
        if fallback.exists():
            input_file = fallback
            print(f"📂 {INPUT_FILE} がありません。{fallback} を使用します。")
        else:
            print(f"❌ {INPUT_FILE} または refle_full_list.json が見つかりません。先に全件取得を実行してください。")
            return

    with open(input_file, "r", encoding="utf-8") as f:
        master_data = json.load(f)

    total = len(master_data)
    purl_shops = [(i, s) for i, s in enumerate(master_data) if s.get("official_url") and "/pUrl/" in s["official_url"]]
    purl_count = len(purl_shops)
    print(f"📋 総店舗数: {total} 件")
    print(f"🔗 /pUrl/ を含む店舗: {purl_count} 件")
    print("=" * 50)

    resolved = 0
    for idx, (i, shop) in enumerate(purl_shops):
        official_url = shop.get("official_url")
        final_url = resolve_url(official_url)
        if final_url and "refle.info" not in final_url:
            shop["official_url"] = final_url
            resolved += 1
            print(f"[{idx + 1}/{purl_count}] {shop.get('shop_name', '不明')}: {final_url[:70]}...")
        else:
            print(f"[{idx + 1}/{purl_count}] {shop.get('shop_name', '不明')}: 解決なし（refle内のまま）")

        time.sleep(REQUEST_DELAY)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(master_data, f, indent=2, ensure_ascii=False)

    print("=" * 50)
    print(f"✅ 完了: {resolved} 件を解決し、{OUTPUT_FILE} に保存しました。")


if __name__ == "__main__":
    main()
