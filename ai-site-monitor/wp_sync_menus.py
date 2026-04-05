import os
import json
import time
import requests
import argparse
from dotenv import load_dotenv

# .envファイルを読み込む（これがないとos.getenvが効きません）
load_dotenv()

# 設定の読み込み
WP_SITE_URL = os.getenv('WP_SITE_URL')
WP_USER = os.getenv('WP_USER')
WP_APP_PASSWORD = os.getenv('WP_APP_PASSWORD')

def sync_menus(dry_run=True):
    # JSONの読み込み
    try:
        with open('scraped_menus.json', 'r', encoding='utf-8') as f:
            shop_data_list = json.load(f)
    except FileNotFoundError:
        print("ERROR: scraped_menus.json が見つかりません。")
        return

    print(f"🚀 {len(shop_data_list)} 店舗の同期を開始します (Dry-run: {dry_run})")

    for i, shop in enumerate(shop_data_list, 1):
        shop_name = shop.get('shop_name')
        menus = shop.get('menus')

        if not shop_name:
            continue

        # 1. WordPressから該当店舗を検索
        # ※投稿タイプが 'shop' であると仮定しています
        search_url = f"{WP_SITE_URL}/wp-json/wp/v2/shop?search={shop_name}"
        
        try:
            # rules.mdに従い、API呼び出し前に待機（負荷軽減）
            time.sleep(1.5) 
            
            response = requests.get(search_url, auth=(WP_USER, WP_APP_PASSWORD))
            posts = response.json()

            if response.status_code == 200 and posts:
                # 最も名前が近いものを選択
                target_post = posts[0]
                post_id = target_post['id']
                
                if dry_run:
                    print(f"[{i}/{len(shop_data_list)}] ✅ マッチ成功（シミュレーション）: {shop_name} (ID: {post_id})")
                else:
                    # 2. ACF（カスタムフィールド）を更新
                    # ai-update-log.php で用意した meta 構造に合わせて送信
                    update_url = f"{WP_SITE_URL}/wp-json/wp/v2/shop/{post_id}"
                    update_data = {
                        "meta": {
                            "refl_menu_data": json.dumps(menus, ensure_ascii=False)
                        }
                    }
                    update_res = requests.post(update_url, json=update_data, auth=(WP_USER, WP_APP_PASSWORD))
                    
                    if update_res.status_code == 200:
                        print(f"[{i}/{len(shop_data_list)}] 🚀 更新完了: {shop_name}")
                    else:
                        print(f"[{i}/{len(shop_data_list)}] ⚠️ 更新失敗: {shop_name} (HTTP {update_res.status_code})")
            else:
                print(f"[{i}/{len(shop_data_list)}] ❌ 未マッチ: {shop_name}")

        except Exception as e:
            print(f"[{i}/{len(shop_data_list)}] ⚠️ エラー発生: {shop_name} -> {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='実際に更新せずにマッチングを確認します')
    args = parser.parse_args()

    if not all([WP_SITE_URL, WP_USER, WP_APP_PASSWORD]):
        print("ERROR: .envの設定が読み込めていません。")
    else:
        sync_menus(dry_run=args.dry_run)