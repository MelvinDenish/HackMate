"""
╔══════════════════════════════════════════════════════════════╗
║  WEB SEARCH TOOL — DuckDuckGo + URL Content Extraction       ║
║                                                              ║
║  Used by: Research Agent (Phase 1)                           ║
║  Provider: No API key required (DuckDuckGo is free)          ║
║                                                              ║
║  Capabilities:                                               ║
║  • Web search via DuckDuckGo                                 ║
║  • URL content fetching and text extraction                  ║
║  • Rate-limited to avoid throttling                          ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
import asyncio
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# ── Rate Limiting ─────────────────────────────────────────────

_search_semaphore = asyncio.Semaphore(3)   # Max 3 concurrent searches
_fetch_semaphore = asyncio.Semaphore(5)    # Max 5 concurrent fetches


# ── Search Functions ──────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def search_web(query: str, max_results: int = 8) -> list[dict]:
    """
    Search the web using DuckDuckGo.

    Args:
        query: Search query string
        max_results: Maximum number of results to return

    Returns:
        List of dicts with keys: title, url, snippet
    """
    logger.info(f"[WebSearch] Searching: {query}")
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))

        formatted = []
        for r in results:
            formatted.append({
                "title": r.get("title", ""),
                "url": r.get("href", r.get("link", "")),
                "snippet": r.get("body", r.get("snippet", "")),
            })

        logger.info(f"[WebSearch] Found {len(formatted)} results for: {query}")
        return formatted

    except Exception as e:
        logger.error(f"[WebSearch] Search failed for '{query}': {e}")
        return []


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def search_news(query: str, max_results: int = 5) -> list[dict]:
    """Search for recent news articles using DuckDuckGo."""
    logger.info(f"[WebSearch] News search: {query}")
    try:
        with DDGS() as ddgs:
            results = list(ddgs.news(query, max_results=max_results))

        formatted = []
        for r in results:
            formatted.append({
                "title": r.get("title", ""),
                "url": r.get("url", r.get("link", "")),
                "snippet": r.get("body", ""),
                "date": r.get("date", ""),
                "source": r.get("source", ""),
            })

        return formatted

    except Exception as e:
        logger.error(f"[WebSearch] News search failed: {e}")
        return []


# ── URL Content Extraction ────────────────────────────────────

@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=5))
def fetch_url_content(url: str, max_chars: int = 8000) -> dict:
    """
    Fetch and extract readable text content from a URL.

    Args:
        url: URL to fetch
        max_chars: Maximum characters to extract

    Returns:
        Dict with keys: url, title, content, success
    """
    logger.info(f"[WebSearch] Fetching: {url}")
    result = {"url": url, "title": "", "content": "", "success": False}

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            )
        }

        with httpx.Client(timeout=15, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        # Extract title
        title_tag = soup.find("title")
        result["title"] = title_tag.get_text(strip=True) if title_tag else ""

        # Remove script, style, nav, footer elements
        for tag in soup(["script", "style", "nav", "footer", "header",
                         "aside", "form", "iframe", "noscript"]):
            tag.decompose()

        # Extract main content (prefer article/main tags)
        main_content = soup.find("article") or soup.find("main") or soup.find("body")
        if main_content:
            text = main_content.get_text(separator="\n", strip=True)
        else:
            text = soup.get_text(separator="\n", strip=True)

        # Clean up: remove excessive whitespace
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)

        result["content"] = text[:max_chars]
        result["success"] = True
        logger.info(
            f"[WebSearch] Extracted {len(result['content'])} chars from {url}"
        )

    except httpx.HTTPStatusError as e:
        logger.warning(f"[WebSearch] HTTP {e.response.status_code} for {url}")
        result["content"] = f"HTTP Error: {e.response.status_code}"

    except Exception as e:
        logger.warning(f"[WebSearch] Fetch failed for {url}: {e}")
        result["content"] = f"Fetch error: {str(e)}"

    return result


def fetch_multiple_urls(
    urls: list[str],
    max_chars_per_url: int = 5000,
) -> list[dict]:
    """
    Fetch content from multiple URLs.

    Args:
        urls: List of URLs to fetch
        max_chars_per_url: Max chars to extract per URL

    Returns:
        List of result dicts
    """
    results = []
    for url in urls[:10]:  # Cap at 10 URLs to avoid abuse
        result = fetch_url_content(url, max_chars=max_chars_per_url)
        results.append(result)
    return results


# ── Research Utilities ────────────────────────────────────────

def research_topic(
    topic: str,
    search_queries: Optional[list[str]] = None,
    max_results_per_query: int = 5,
    fetch_top_n: int = 3,
) -> dict:
    """
    Conduct comprehensive research on a topic.

    1. Run multiple search queries
    2. Collect unique URLs
    3. Fetch top N pages for full content
    4. Return structured research data

    Args:
        topic: Main research topic
        search_queries: Optional specific queries (auto-generated if None)
        max_results_per_query: Results per search query
        fetch_top_n: Number of top URLs to fetch full content from

    Returns:
        Dict with: topic, search_results, fetched_pages, summary_stats
    """
    if search_queries is None:
        search_queries = [
            topic,
            f"{topic} market size trends 2025 2026",
            f"{topic} competitors analysis",
            f"{topic} technology stack implementation",
        ]

    all_results = []
    seen_urls = set()

    for query in search_queries:
        results = search_web(query, max_results=max_results_per_query)
        for r in results:
            if r["url"] not in seen_urls:
                seen_urls.add(r["url"])
                all_results.append(r)

    # Fetch full content from top results
    top_urls = [r["url"] for r in all_results[:fetch_top_n]]
    fetched = fetch_multiple_urls(top_urls)

    return {
        "topic": topic,
        "queries_executed": search_queries,
        "search_results": all_results,
        "fetched_pages": [p for p in fetched if p["success"]],
        "stats": {
            "total_results": len(all_results),
            "pages_fetched": len([p for p in fetched if p["success"]]),
            "unique_sources": len(seen_urls),
        },
    }
