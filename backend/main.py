"""
Pro SEO Analyzer Backend
Google検索上位10サイトを深層分析し、SEO戦略レポートを生成
"""

import asyncio
import json
import re
from collections import Counter
from typing import Optional
from urllib.parse import urlparse

import httpx
import trafilatura
from anthropic import Anthropic
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import os

# 日本語形態素解析（オプション）
try:
    from janome.tokenizer import Tokenizer
    JANOME_AVAILABLE = True
except ImportError:
    JANOME_AVAILABLE = False

load_dotenv()

app = FastAPI(
    title="Pro SEO Analyzer API",
    description="競合上位サイトを分析し、勝てるSEO戦略を生成",
    version="1.0.0"
)

# CORS設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 環境変数
SERPER_API_KEY = os.getenv("SERPER_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")


# ============ Pydantic Models ============

class AnalyzeRequest(BaseModel):
    keyword: str = Field(..., min_length=1, max_length=200, description="分析するキーワード")
    language: str = Field(default="ja", description="検索言語")
    country: str = Field(default="jp", description="検索国")


class HeadingStructure(BaseModel):
    h1: list[str] = []
    h2: list[str] = []
    h3: list[str] = []


class CompetitorData(BaseModel):
    rank: int
    url: str
    title: str
    domain: str
    word_count: int
    headings: HeadingStructure
    snippet: str = ""


class ContentStatistics(BaseModel):
    average_word_count: int
    max_word_count: int
    min_word_count: int
    median_word_count: int
    top_keywords: list[dict]


class AIAnalysis(BaseModel):
    user_intent: dict
    winning_structure: list[dict]
    content_gaps: list[str]
    required_keywords: list[str]
    recommended_word_count: int
    difficulty_score: int = Field(ge=1, le=10)
    summary: str


class AnalyzeResponse(BaseModel):
    keyword: str
    competitors: list[CompetitorData]
    statistics: ContentStatistics
    ai_analysis: AIAnalysis
    status: str = "success"


# ============ Helper Functions ============

async def search_serper(keyword: str, language: str = "ja", country: str = "jp") -> list[dict]:
    """Serper APIで検索上位10件を取得"""
    if not SERPER_API_KEY:
        raise HTTPException(status_code=500, detail="SERPER_API_KEY not configured")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY": SERPER_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "q": keyword,
                "gl": country,
                "hl": language,
                "num": 15  # 余裕を持って取得
            },
            timeout=30.0
        )

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail="Search API error")

        data = response.json()
        organic_results = data.get("organic", [])

        # PDFと広告を除外し、上位10件に絞る
        filtered = []
        for item in organic_results:
            url = item.get("link", "")
            if url.endswith(".pdf") or "googleadservices" in url:
                continue
            filtered.append({
                "url": url,
                "title": item.get("title", ""),
                "snippet": item.get("snippet", "")
            })
            if len(filtered) >= 10:
                break

        return filtered


async def scrape_page(url: str, timeout: float = 15.0) -> dict:
    """単一ページをスクレイピング"""
    result = {
        "url": url,
        "title": "",
        "content": "",
        "word_count": 0,
        "headings": {"h1": [], "h2": [], "h3": []},
        "success": False
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            response = await client.get(url, headers=headers, timeout=timeout)

            if response.status_code != 200:
                return result

            html = response.text

            # trafilaturaで本文抽出
            content = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                no_fallback=False
            )

            # BeautifulSoupでHTML構造解析
            soup = BeautifulSoup(html, "lxml")

            # タイトル取得
            title_tag = soup.find("title")
            result["title"] = title_tag.get_text(strip=True) if title_tag else ""

            # 見出し抽出
            for tag in ["h1", "h2", "h3"]:
                elements = soup.find_all(tag)
                result["headings"][tag] = [
                    el.get_text(strip=True)[:100]  # 長すぎる見出しは切り詰め
                    for el in elements
                    if el.get_text(strip=True)
                ][:20]  # 最大20個まで

            # 本文とワードカウント
            if content:
                result["content"] = content
                # 日本語の文字数カウント（空白除去）
                clean_content = re.sub(r'\s+', '', content)
                result["word_count"] = len(clean_content)

            result["success"] = True

    except Exception as e:
        print(f"Scraping error for {url}: {str(e)}")

    return result


async def scrape_all_pages(urls: list[dict]) -> list[dict]:
    """全ページを並列スクレイピング"""
    tasks = [scrape_page(item["url"]) for item in urls]
    results = await asyncio.gather(*tasks)

    # 元の検索結果とマージ
    for i, result in enumerate(results):
        if i < len(urls):
            result["snippet"] = urls[i].get("snippet", "")
            if not result["title"]:
                result["title"] = urls[i].get("title", "")

    return results


def calculate_statistics(scraped_data: list[dict]) -> dict:
    """統計情報を算出"""
    word_counts = [d["word_count"] for d in scraped_data if d["success"] and d["word_count"] > 0]

    if not word_counts:
        return {
            "average_word_count": 0,
            "max_word_count": 0,
            "min_word_count": 0,
            "median_word_count": 0,
            "top_keywords": []
        }

    sorted_counts = sorted(word_counts)
    median = sorted_counts[len(sorted_counts) // 2]

    # 頻出キーワード抽出
    all_content = " ".join([d["content"] for d in scraped_data if d["success"] and d["content"]])
    top_keywords = extract_keywords(all_content)

    return {
        "average_word_count": int(sum(word_counts) / len(word_counts)),
        "max_word_count": max(word_counts),
        "min_word_count": min(word_counts),
        "median_word_count": median,
        "top_keywords": top_keywords
    }


def extract_keywords(text: str, top_n: int = 20) -> list[dict]:
    """テキストから重要キーワードを抽出"""
    if not text:
        return []

    if JANOME_AVAILABLE:
        # Janomeで日本語形態素解析
        tokenizer = Tokenizer()
        tokens = tokenizer.tokenize(text)

        # 名詞・動詞のみ抽出
        words = []
        for token in tokens:
            part = token.part_of_speech.split(',')[0]
            if part in ['名詞', '動詞']:
                surface = token.surface
                if len(surface) >= 2:  # 2文字以上
                    words.append(surface)
    else:
        # フォールバック: 簡易的な抽出
        # 英数字とひらがな・カタカナ・漢字を抽出
        words = re.findall(r'[\u4e00-\u9faf\u3040-\u309f\u30a0-\u30ff]{2,}|[a-zA-Z]{3,}', text)

    # ストップワード除去
    stopwords = {'これ', 'それ', 'あれ', 'この', 'その', 'あの', 'こと', 'もの', 'ため', 'よう', 'など',
                 'する', 'いる', 'ある', 'なる', 'できる', 'れる', 'られる', 'です', 'ます'}
    words = [w for w in words if w not in stopwords]

    # 頻度カウント
    counter = Counter(words)
    top_words = counter.most_common(top_n)

    return [{"word": word, "count": count} for word, count in top_words]


async def analyze_with_claude(keyword: str, competitors_data: list[dict], statistics: dict) -> dict:
    """Claude 3.5 Sonnetで戦略分析"""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    # 競合データを整形
    competitors_summary = []
    for i, comp in enumerate(competitors_data, 1):
        if comp["success"]:
            competitors_summary.append({
                "rank": i,
                "title": comp["title"][:100],
                "word_count": comp["word_count"],
                "h1": comp["headings"]["h1"][:3],
                "h2": comp["headings"]["h2"][:10],
                "h3": comp["headings"]["h3"][:10],
                "content_preview": comp["content"][:1500] if comp["content"] else ""
            })

    system_prompt = """あなたは世界トップクラスのSEOストラテジストです。渡された競合上位サイトのデータに基づき、このキーワードで確実に1位を取るための『勝てる構成案』を作成してください。

以下のJSON形式で出力すること（他の文章は一切不要）:
{
  "user_intent": {
    "explicit_needs": "ユーザーの顕在ニーズ（直接的な目的）",
    "implicit_needs": "ユーザーの潜在ニーズ（深層心理、本当に求めていること）",
    "search_stage": "カスタマージャーニーのどの段階か（認知/検討/比較/購入）"
  },
  "winning_structure": [
    {
      "h2": "見出し例",
      "h3_list": ["小見出し1", "小見出し2"],
      "key_points": "この章で必ず触れるべきポイント"
    }
  ],
  "content_gaps": ["上位サイトが網羅できていないニッチなトピック1", "トピック2"],
  "required_keywords": ["必須キーワード1", "必須キーワード2"],
  "recommended_word_count": 5000,
  "difficulty_score": 7,
  "summary": "この戦略で勝てる理由を100字程度で説明"
}"""

    user_prompt = f"""# 分析対象キーワード
「{keyword}」

# 競合上位サイトデータ
{json.dumps(competitors_summary, ensure_ascii=False, indent=2)}

# 統計情報
- 平均文字数: {statistics['average_word_count']}
- 最大文字数: {statistics['max_word_count']}
- 頻出キーワード: {json.dumps(statistics['top_keywords'][:10], ensure_ascii=False)}

上記データを分析し、このキーワードで1位を取るための戦略をJSON形式で出力してください。"""

    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[
            {"role": "user", "content": user_prompt}
        ],
        system=system_prompt
    )

    response_text = message.content[0].text

    # JSON抽出（マークダウンコードブロック対応）
    json_match = re.search(r'```json?\s*([\s\S]*?)\s*```', response_text)
    if json_match:
        response_text = json_match.group(1)

    try:
        analysis = json.loads(response_text)
    except json.JSONDecodeError:
        # フォールバック
        analysis = {
            "user_intent": {
                "explicit_needs": "データ解析エラー",
                "implicit_needs": "再試行してください",
                "search_stage": "不明"
            },
            "winning_structure": [],
            "content_gaps": [],
            "required_keywords": [],
            "recommended_word_count": statistics.get("average_word_count", 3000),
            "difficulty_score": 5,
            "summary": "AI分析でエラーが発生しました。再度お試しください。"
        }

    return analysis


# ============ API Endpoints ============

@app.get("/")
async def root():
    return {"message": "Pro SEO Analyzer API", "version": "1.0.0"}


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "serper_configured": bool(SERPER_API_KEY),
        "anthropic_configured": bool(ANTHROPIC_API_KEY)
    }


@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze_keyword(request: AnalyzeRequest):
    """
    キーワードを分析し、SEO戦略レポートを生成

    1. Serper APIで検索上位10件を取得
    2. 各サイトを並列スクレイピング
    3. 統計情報を算出
    4. Claude 3.5 Sonnetで戦略分析
    """
    keyword = request.keyword.strip()

    # Step 1: 検索
    search_results = await search_serper(keyword, request.language, request.country)

    if not search_results:
        raise HTTPException(status_code=404, detail="No search results found")

    # Step 2: スクレイピング
    scraped_data = await scrape_all_pages(search_results)

    # Step 3: 統計算出
    statistics = calculate_statistics(scraped_data)

    # Step 4: AI分析
    ai_analysis = await analyze_with_claude(keyword, scraped_data, statistics)

    # レスポンス構築
    competitors = []
    for i, data in enumerate(scraped_data, 1):
        parsed_url = urlparse(data["url"])
        competitors.append(CompetitorData(
            rank=i,
            url=data["url"],
            title=data["title"] or f"Rank {i}",
            domain=parsed_url.netloc,
            word_count=data["word_count"],
            headings=HeadingStructure(**data["headings"]),
            snippet=data.get("snippet", "")
        ))

    return AnalyzeResponse(
        keyword=keyword,
        competitors=competitors,
        statistics=ContentStatistics(**statistics),
        ai_analysis=AIAnalysis(**ai_analysis),
        status="success"
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
