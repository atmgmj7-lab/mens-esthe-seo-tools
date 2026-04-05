#!/usr/bin/env python3
"""
ai-site-monitor: 1,000サイト巡回・ハッシュ判定・Gemini解析

- sites.json のURLを巡回
- 本文テキストのハッシュで変更検知
- 変更あり → Geminiで要約
- 結果を results/ に保存、Discord通知（任意）
"""

import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from google import genai

# ============================================================
# 設定
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
SITES_JSON = BASE_DIR / "sites.json"
HASHES_JSON = BASE_DIR / "data" / "hashes.json"
RESULTS_DIR = BASE_DIR / "results"
DATA_DIR = BASE_DIR / "data"

# 1リクエストあたりの待機秒数（レート制限対策）
REQUEST_DELAY = 1.0
# 本文の最大文字数（トークン節約）
MAX_TEXT_LEN = 8000


def ensure_dirs():
    """必要なディレクトリを作成"""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_sites() -> list[str]:
    """sites.json からURLリストを読み込み"""
    with open(SITES_JSON, encoding="utf-8") as f:
        return json.load(f)


def load_hashes() -> dict[str, str]:
    """前回のハッシュ一覧を読み込み"""
    if not HASHES_JSON.exists():
        return {}
    try:
        with open(HASHES_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_hashes(hashes: dict[str, str]):
    """ハッシュ一覧を保存"""
    with open(HASHES_JSON, "w", encoding="utf-8") as f:
        json.dump(hashes, f, indent=2, ensure_ascii=False)


def fetch_and_extract(url: str) -> str | None:
    """URLを取得し、本文テキストを抽出"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AISiteMonitor/1.0)",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ja,en;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        main = soup.find("main") or soup.find("article") or soup.find("body") or soup
        text = main.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text))
        return text[:MAX_TEXT_LEN] if text else None
    except Exception as e:
        print(f"  [SKIP] {url}: {e}")
        return None


def compute_hash(text: str) -> str:
    """テキストのSHA256ハッシュを計算"""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def analyze_with_gemini(url: str, text: str) -> str:
    """Gemini APIで変更内容を要約"""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "GEMINI_API_KEY が設定されていません"

    client = genai.Client(api_key=api_key)
    models = ["models/gemini-2.5-flash", "models/gemini-flash-latest"]
    prompt = f"""以下のURLのページ内容が変更されました。変更点を200文字程度で要約してください。

URL: {url}

【抽出テキスト】
{text[:6000]}

【出力】変更の要点のみを簡潔に。"""

    for model in models:
        try:
            response = client.models.generate_content(model=model, contents=prompt)
            return (response.text or "").strip()
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                print("    [429] 15秒待機してリトライ...")
                time.sleep(15)
                continue
            return f"解析エラー: {e}"
    return "解析失敗"


def notify_discord(message: str):
    """Discord Webhook に通知"""
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        return
    try:
        payload = {"content": message[:2000]}
        requests.post(webhook, json=payload, timeout=10)
    except Exception as e:
        print(f"  [Discord] 通知失敗: {e}")


def main():
    ensure_dirs()
    sites = load_sites()
    hashes = load_hashes()
    changed_sites: list[dict] = []
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"[ai-site-monitor] {len(sites)} サイト巡回開始")

    for i, url in enumerate(sites):
        if (i + 1) % 100 == 0:
            print(f"  進捗: {i + 1}/{len(sites)}")

        time.sleep(REQUEST_DELAY)
        text = fetch_and_extract(url)
        if not text:
            continue

        new_hash = compute_hash(text)
        old_hash = hashes.get(url)

        if old_hash and old_hash != new_hash:
            print(f"  [CHANGE] {url}")
            summary = analyze_with_gemini(url, text)
            changed_sites.append({
                "url": url,
                "summary": summary,
                "detected_at": run_id,
            })
            notify_discord(f"🔔 変更検知: {url}\n{summary[:300]}...")

        hashes[url] = new_hash

    save_hashes(hashes)

    # 結果を保存
    result_file = RESULTS_DIR / f"changes_{run_id}.json"
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump({
            "run_id": run_id,
            "total_sites": len(sites),
            "changed_count": len(changed_sites),
            "changed_sites": changed_sites,
        }, f, indent=2, ensure_ascii=False)

    print(f"\n[完了] 変更検知: {len(changed_sites)} 件")
    print(f"  結果: {result_file}")


if __name__ == "__main__":
    main()
