import requests
from bs4 import BeautifulSoup
import time
import json
import re
from urllib.parse import urljoin

# ==========================================
# ターゲット設定
# ==========================================
# ユーザー指定のURLベースを使用
AREA_BASE_URL = "https://osaka.refle.info/G0000/"
BASE_URL = "https://osaka.refle.info"

def get_all_shop_links():
    """
    ?page=0〜?page=16 で全ページを走査し、約400店舗のベースURLを抽出。
    ※ 実測: page 0-8 で423件取得可能。page 9-16 は空のためスキップ。
    """
    print("🔍 ?page= 方式で全店舗のリンクを探索します...")
    shop_links = set()
    empty_streak = 0
    EXCLUDE_IDS = {"shop_news", "shop_cpon", "shop_menu"}

    for p in range(0, 18):
        page_url = f"{AREA_BASE_URL}?page={p}"
        print(f" [{p}ページ目] 取得中: {page_url}")

        try:
            if p > 0:
                time.sleep(2)
            response = requests.get(
                page_url,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            if response.status_code != 200:
                print(f"  -> HTTP {response.status_code}")
                break

            soup = BeautifulSoup(response.text, "html.parser")

            # 方法1: div.salondata 内の /shop/xxx/ リンクを優先
            found = 0
            for block in soup.select("div.salondata"):
                for a in block.find_all("a", href=True):
                    href = a["href"]
                    m = re.search(r"^/shop/([^/]+)/?$", href)
                    if m and m.group(1) not in EXCLUDE_IDS:
                        full = urljoin(BASE_URL, f"/shop/{m.group(1)}/")
                        if full not in shop_links:
                            shop_links.add(full)
                            found += 1
                        break  # 1ブロック1店舗

            # 方法2: salondataが0件の場合はページ全体から抽出
            if found == 0:
                for a in soup.find_all("a", href=True):
                    m = re.search(r"^/shop/([^/]+)/?$", a["href"])
                    if m and m.group(1) not in EXCLUDE_IDS:
                        full = urljoin(BASE_URL, f"/shop/{m.group(1)}/")
                        if full not in shop_links:
                            shop_links.add(full)
                            found += 1

            print(f"  -> {found} 件追加 (累計: {len(shop_links)})")

            if found == 0:
                empty_streak += 1
                if empty_streak >= 2 and len(shop_links) > 0:
                    print("  -> 2ページ連続で0件のため終了")
                    break
            else:
                empty_streak = 0

        except Exception as e:
            print(f"⚠️ エラー: {e}")
            break

    return sorted(list(shop_links))

def scrape_shop_menus(base_url):
    """詳細ページの料金テーブルと『正しい店名』を抽出"""
    menu_url = urljoin(base_url, "shop_menu.html")
    try:
        response = requests.get(menu_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if response.status_code != 200: return None
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 🌟 店名取得ロジックの改善
        shop_name = ""
        # 1. まずは .shopNameIn を探す（最優先）
        shop_name_element = soup.select_one('.shopNameIn')
        if shop_name_element:
            shop_name = shop_name_element.get_text(strip=True)
        else:
            # 2. 見つからない場合は、以前のパンくずやH1を予備にする
            breadcrumb = soup.select_one('#breadcrumb ul li:last-child')
            if breadcrumb:
                shop_name = breadcrumb.get_text(strip=True).replace('のメニュー・料金', '').replace('メニュー・料金', '')
            else:
                h1 = soup.find('h1')
                shop_name = h1.get_text(strip=True).split('｜')[0].split('のメ')[0] if h1 else ""
            
        menus = []
        # セレクター指定
        target = soup.select_one('#container > div:nth-child(14)') or soup.find('div', class_='salondata') or soup

        for table in target.find_all('table'):
            prices = []
            for row in table.find_all('tr'):
                th, td = row.find('th'), row.find('td')
                if th and td:
                    prices.append({"time": th.get_text(strip=True), "price": td.get_text(strip=True)})
            if prices:
                menus.append({"course_title": "メニュー", "prices": prices})
                
        return {"shop_name": shop_name, "url": menu_url, "menus": menus} if menus else None
    except:
        return None

def main():
    links = get_all_shop_links()
    if not links:
        print("店舗が見つかりませんでした。URLやネット接続を確認してください。")
        return

    print(f"\n🚀 合計 {len(links)} 店舗の詳細データ抽出を開始します。")
    results = []
    
    for i, link in enumerate(links, 1):
        try:
            data = scrape_shop_menus(link)
            if data:
                results.append(data)
                print(f"[{i}/{len(links)}] ✅ {data['shop_name']}")
            else:
                print(f"[{i}/{len(links)}] ❌ 料金なし: {link}")
        except Exception as e:
            print(f"[{i}/{len(links)}] ⚠️ エラー発生（スキップ）: {e}")
        
        # 10件ごとに保存
        if i % 10 == 0:
            with open("scraped_menus.json", "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
        
        # サーバー負荷対策
        time.sleep(1.5)

    with open("scraped_menus.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n🎉 完了！全 {len(results)} 店舗のデータを scraped_menus.json に保存しました。")

if __name__ == "__main__":
    main()
