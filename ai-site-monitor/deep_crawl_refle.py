import asyncio
import json
import os
import re
from pathlib import Path
from bs4 import BeautifulSoup

_BASE = Path(__file__).resolve().parent
BASE_URL = "https://osaka.refle.info/G0000/"
OUTPUT_FILE = _BASE / "refle_full_raw_840.json"

async def main_async():
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
    except ImportError:
        print("❌ crawl4ai が見つかりません。")
        return

    print("\n🚀 [START] 1ページから17ページまで順番に全件取得を開始します...")
    
    browser_cfg = BrowserConfig(browser_type="chromium", headless=False)
    run_cfg = CrawlerRunConfig(cache_mode="bypass")
    
    all_results = []
    
    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        # 1から17ページまで順番にループ
        for page in range(1, 18): 
            current_url = f"{BASE_URL}?page={page}"
            print(f"\n--- 📄 {page}/17 ページ目を解析中 (現在の累計: {len(all_results)} 件) ---")
            
            # ページ読み込み
            result = await crawler.arun(url=current_url, config=run_cfg)
            if not result.success:
                print(f"⚠️ ページ {page} の取得に失敗しました。スキップします。")
                continue

            soup = BeautifulSoup(result.html, "html.parser")
            shops = soup.select("div.salondata")
            
            # 店舗が1件も見つからなかった場合
            if not shops:
                print(f"⚠️ ページ {page} に店舗が見つかりませんでした。サイトの制限か読み込みエラーの可能性があります。")
                # 念のため、1回だけ再試行（リロード）
                await asyncio.sleep(3)
                result = await crawler.arun(url=current_url, config=run_cfg)
                soup = BeautifulSoup(result.html, "html.parser")
                shops = soup.select("div.salondata")
                if not shops:
                    print(f"⏩ 2回試しても見つからないため、このページをスキップして次へ進みます。")
                    continue

            # 店舗情報の抽出
            for shop in shops:
                # 店名
                name_el = shop.select_one("h4.salondata_salonname a")
                if not name_el: continue
                temp_soup = BeautifulSoup(str(name_el), "html.parser")
                for span in temp_soup.find_all("span"): span.decompose()
                shop_name = temp_soup.get_text(strip=True)

                # 公式サイトURL (リダイレクト解決は後で別スクリプトで行う)
                official_el = shop.select_one("a.salondata_right_site")
                off_url = None
                if official_el and "noactive" not in official_el.get("class", []):
                    off_url = official_el.get("href")
                    if off_url and off_url.startswith("/"):
                        off_url = f"https://osaka.refle.info{off_url}"

                all_results.append({
                    "shop_name": shop_name,
                    "official_url": off_url,
                    "phone": shop.select_one("span.salondata_tel b").get_text(strip=True) if shop.select_one("span.salondata_tel b") else "",
                    "address": shop.select_one("div.salondata_area p").get_text(strip=True) if shop.select_one("div.salondata_area p") else "",
                    "hours": re.sub(r"^営業時間[:：]", "", shop.select_one("span.salondata_time").get_text(strip=True)) if shop.select_one("span.salondata_time") else ""
                })

            # ページ間の待機時間を少し長め（3秒）にして安定させる
            await asyncio.sleep(3)

        # 最終保存
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)
        
        print(f"\n🏁 [完了] 17ページの巡回を終えました。合計 {len(all_results)} 件を保存しました。")

if __name__ == "__main__":
    asyncio.run(main_async())