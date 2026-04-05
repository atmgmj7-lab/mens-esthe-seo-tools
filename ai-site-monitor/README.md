# ai-site-monitor

1,000サイトを巡回し、変更を検知してGeminiで要約する監視システム。GitHub Actionsで毎日自動実行。

**Public リポジトリ**にすると GitHub Actions が無料で使い放題です。

## セットアップ

### 0. リポジトリの作成

1. GitHub で新規リポジトリ（例: `ai-site-monitor`）を作成
2. **Public** に設定
3. このフォルダの内容をリポジトリ直下に push

### 1. GitHub Secrets の設定

リポジトリの **Settings > Secrets and variables > Actions** で以下を登録：

| Secret | 説明 |
|--------|------|
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/) で取得したAPIキー（必須） |
| `DISCORD_WEBHOOK_URL` | Discord通知用Webhook URL（任意） |

### 2. 監視URLの設定

`sites.json` を編集し、監視したいURLのリストに差し替えてください。

```json
[
  "https://example.com/page1",
  "https://example.com/page2"
]
```

### 3. 実行

- **自動**: 毎日 09:00 JST（00:00 UTC）に実行
- **手動**: Actions タブから「Daily Site Monitor」→「Run workflow」

## 出力

- `data/hashes.json` … 各URLの前回ハッシュ（変更検知用）
- `results/changes_YYYYMMDD_HHMMSS.json` … 変更があったサイトの要約

## ローカル実行

```bash
pip install -r requirements.txt
export GEMINI_API_KEY="your-key"
python main.py
```

## WordPress REST API テスト（test_get_urls.py）

WordPress に登録された shop 投稿の店名・公式URLを取得するテストスクリプト。

### 前提条件

1. **functions.php** に REST API で `official_url` を公開する処理が追加されていること
2. WordPress で **Application Passwords** を有効化し、パスワードを発行済みであること

### セットアップ

```bash
cd ai-site-monitor
cp .env.example .env
# .env を編集して WP_SITE_URL, WP_USER, WP_APP_PASSWORD を設定
```

### 実行

```bash
python test_get_urls.py
```

### 環境変数

| 変数 | 説明 |
|------|------|
| `WP_SITE_URL` | WordPress サイトURL（例: https://example.com） |
| `WP_USER` | 管理者ユーザー名 |
| `WP_APP_PASSWORD` | Application Password（ユーザー > プロフィール > アプリケーションパスワードで発行） |

### RequestsDependencyWarning の解消

`urllib3` や `chardet` のバージョン警告が出る場合：

```bash
pip install --upgrade requests urllib3
```

または、互換バージョンを明示的に指定：

```bash
pip install "requests>=2.31.0" "urllib3>=2.0.0,<3.0.0"
```

## AI クローラー（crawler_base.py）

WordPress から店舗リストを取得し、Playwright で公式サイトを巡回するベーススクリプト。

### 前提条件

- `test_get_urls.py` と同様に `.env` で WordPress 認証を設定済みであること
- Playwright のブラウザバイナリがインストール済みであること

### セットアップ（初回のみ）

```bash
cd ai-site-monitor

# 1. パッケージインストール
pip install -r requirements.txt

# 2. Playwright のブラウザバイナリをインストール（Chromium）
playwright install
```

### 実行

```bash
python crawler_base.py
```

### 動作

1. WordPress REST API から shop 投稿一覧を取得
2. URL が有効な店舗のうち、**最初の 3 件**を巡回対象とする
3. 各サイトにアクセスし、`<title>` を取得してターミナルに表示
4. スクリーンショットを `screenshots/[post_id]_[店舗名].png` に保存
5. タイムアウト・接続エラー時はエラーを表示して次の店舗へ継続

### 巡回件数の変更

`crawler_base.py` の先頭で `CRAWL_LIMIT = 3` を変更してください。

## AI 自動更新（ai_auto_updater.py）

店舗サイトを巡回し、Gemini で要約を生成して WordPress の ACF `shop_ai_summary` に保存するスクリプト。

### 前提条件

- `.env` に `WP_SITE_URL`, `WP_USER`, `WP_APP_PASSWORD`, `GEMINI_API_KEY` が設定済み
- `ai-update-log.php` が読み込まれ、`shop_ai_summary` の更新に対応していること

### パッケージ

```bash
pip install playwright requests python-dotenv google-genai
playwright install
```

### 実行

```bash
python ai_auto_updater.py
```

### 処理フロー

1. WordPress REST API から URL が有効な店舗を 3 件取得
2. Playwright で各サイトの body 内テキストを抽出
3. Gemini API で 200〜300 文字の紹介文を生成
4. `POST /wp-json/ai-engine/v1/update` で `shop_ai_summary` を更新

### エラー時

タイムアウト・Gemini エラー時はその店舗をスキップし、次の店舗へ継続します。
