#!/usr/bin/env python3
import asyncio
import json
import os
from pathlib import Path

# パス設定
_BASE = Path(__file__).resolve().parent
INPUT_FILE = _BASE / "refle_full_list.json"

async def main_async():
    print("🚀 [START] 公式サイトのURL解決テストを開始します...")

    if not INPUT_FILE.exists():
        print(f"❌ {INPUT_FILE} が見つかりません。先に全件取得を実行してください。")
        return

    # 1. 取得済みのリストを読み込む
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        master_data = json.load(f)

    # 2. 公式サイトURL(pUrl)がある店舗だけを抽出（テスト用に5件）
    targets = [s for s in master_data if s.get("official_url") and "/pUrl/" in s["official_url"]][:5]

    if not targets:
        print("❌ テスト対象（公式サイトURLありの店舗）が見つかりませんでした。")
        return

    print(f"✅ {len(targets)} 件の店舗でリダイレクト解決をテストします。")

    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
    except ImportError:
        print("❌ crawl4ai が見つかりません。")
        return

    browser_cfg = BrowserConfig(browser_type="chromium", headless=False)
    run_cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=10000)

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        for shop in targets:
            print(f"\n--- 🏥 店名: {shop['shop_name']} ---")
            print(f"🔗 転送前 (リフナビ): {shop['official_url']}")

            try:
                res = await crawler.arun(
                    url=shop["official_url"],
                    config=run_cfg,
                )

                if res.success:
                    final_url = res.url
                    if "refle.info" not in final_url:
                        print(f"✨ 解決成功! 本当の公式サイト: {final_url}")
                    else:
                        print(f"⚠️ 転送先もリフナビ内でした（公式サイト実体なしの可能性）: {final_url}")
                else:
                    print(f"❌ アクセス失敗: {res.error_message}")

            except Exception as e:
                print(f"⚠️ エラー発生: {e}")

            await asyncio.sleep(1)

    print("\n🏁 テスト終了。")


if __name__ == "__main__":
    asyncio.run(main_async())
