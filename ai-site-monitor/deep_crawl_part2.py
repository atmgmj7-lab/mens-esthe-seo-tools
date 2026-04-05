#!/usr/bin/env python3
"""
後半戦: 9〜17ページ目の店舗一覧取得
- 419件の壁を突破するための分割作戦
"""
import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

_BASE = Path(__file__).resolve().parent
REFLE_BASE = "https://osaka.refle.info"
LIST_BASE = f"{REFLE_BASE}/G0000/"
OUTPUT_FILE = _BASE / "refle_part2_results.json"
PAGE_DELAY = 4
START_PAGE = 9
END_PAGE = 17


async def main_async():
    print(f"実行中のファイル: {os.path.abspath(__file__)}")

    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
    except ImportError:
        print("crawl4ai が見つかりません。")
        return

    print(f"\n[START] 後半戦: {START_PAGE}〜{END_PAGE} ページ目の取得を開始します...")

    browser_cfg = BrowserConfig(browser_type="chromium", headless=False)
    run_cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)

    all_results = []

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        for page in range(START_PAGE, END_PAGE + 1):
            url = f"{LIST_BASE}?page={page}"

            result = await crawler.arun(url=url, config=run_cfg)
            if not result.success:
                print(f"[{page}/{END_PAGE}ページ目] 取得失敗: {result.error_message}")
                await asyncio.sleep(PAGE_DELAY)
                continue

            soup = BeautifulSoup(result.html, "html.parser")
            shops = soup.select("div.salondata")

            if not shops:
                print(f"[{page}/{END_PAGE}ページ目] 店舗が0件。次のページを試します。")
                await asyncio.sleep(PAGE_DELAY)
                continue

            for shop in shops:
                name_el = shop.select_one("h4.salondata_salonname a")
                if name_el:
                    temp_soup = BeautifulSoup(str(name_el), "html.parser")
                    for span in temp_soup.find_all("span"):
                        span.decompose()
                    shop_name = temp_soup.get_text(strip=True)
                else:
                    shop_name = "不明"

                official_el = shop.select_one("a.salondata_right_site")
                official_url = None
                if official_el:
                    classes = official_el.get("class") or []
                    if "noactive" not in classes:
                        href = official_el.get("href")
                        if href:
                            official_url = urljoin(REFLE_BASE, href)

                phone_el = shop.select_one("span.salondata_tel b")
                phone = phone_el.get_text(strip=True) if phone_el else ""

                area_el = shop.select_one("div.salondata_area p")
                address = area_el.get_text(strip=True) if area_el else ""

                time_el = shop.select_one("span.salondata_time")
                hours = ""
                if time_el:
                    hours = re.sub(r"^営業時間[:：]\s*", "", time_el.get_text(strip=True))

                all_results.append({
                    "shop_name": shop_name,
                    "official_url": official_url,
                    "phone": phone,
                    "address": address,
                    "hours": hours,
                })

            print(f"[{page}/{END_PAGE}ページ目] このページで {len(shops)} 件、累計 {len(all_results)} 件取得")
            await asyncio.sleep(PAGE_DELAY)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)

    print(f"\n[完了] {len(all_results)} 件を {OUTPUT_FILE} に保存しました。")


if __name__ == "__main__":
    asyncio.run(main_async())
