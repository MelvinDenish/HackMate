"""
╔══════════════════════════════════════════════════════════════╗
║  EXA SEARCH — AI-Native Web Search + Deep Scraping           ║
║                                                              ║
║  Pattern: deep-research (ECC skill)                          ║
║  MCP: exa-web-search from ECC mcp-configs                    ║
║                                                              ║
║  Exa advantages over DuckDuckGo:                             ║
║  ✅ AI-native semantic search (not keyword matching)         ║
║  ✅ Date filtering (only recent results)                     ║
║  ✅ Full page content extraction (not snippets)              ║
║  ✅ Higher quality results for technical topics              ║
║                                                              ║
║  Falls back to DuckDuckGo when EXA_API_KEY not set.          ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)

EXA_BASE_URL = "https://api.exa.ai"


class ExaSearch:
    """AI-native web search via Exa API."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self.available = bool(api_key)

    def search(
        self,
        query: str,
        num_results: int = 8,
        start_date: Optional[str] = None,
        include_text: bool = True,
    ) -> list[dict]:
        """
        Search the web using Exa's AI-native search.

        Args:
            query: Search query
            num_results: Number of results to return
            start_date: Filter for results after this date (ISO format, e.g. "2025-01-01")
            include_text: Whether to include page text content

        Returns:
            List of dicts with url, title, text, publishedDate
        """
        if not self.available:
            logger.warning("[Exa] API key not set, returning empty results")
            return []

        try:
            headers = {
                "x-api-key": self.api_key,
                "Content-Type": "application/json",
            }

            payload = {
                "query": query,
                "numResults": num_results,
                "type": "auto",
                "contents": {},
            }

            if include_text:
                payload["contents"]["text"] = {"maxCharacters": 5000}

            if start_date:
                payload["startPublishedDate"] = start_date

            response = requests.post(
                f"{EXA_BASE_URL}/search",
                json=payload,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            results = []
            for item in data.get("results", []):
                results.append({
                    "url": item.get("url", ""),
                    "title": item.get("title", ""),
                    "text": item.get("text", ""),
                    "published_date": item.get("publishedDate", ""),
                    "score": item.get("score", 0),
                })

            logger.info(f"[Exa] Search returned {len(results)} results for: {query[:60]}")
            return results

        except Exception as e:
            logger.error(f"[Exa] Search failed: {e}")
            return []

    def get_contents(self, urls: list[str], max_chars: int = 5000) -> list[dict]:
        """
        Get full page content for specific URLs.

        Args:
            urls: List of URLs to fetch content from
            max_chars: Maximum characters per page

        Returns:
            List of dicts with url, title, text
        """
        if not self.available:
            return []

        try:
            headers = {
                "x-api-key": self.api_key,
                "Content-Type": "application/json",
            }

            payload = {
                "ids": urls,
                "text": {"maxCharacters": max_chars},
            }

            response = requests.post(
                f"{EXA_BASE_URL}/contents",
                json=payload,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            results = []
            for item in data.get("results", []):
                results.append({
                    "url": item.get("url", ""),
                    "title": item.get("title", ""),
                    "text": item.get("text", ""),
                })

            logger.info(f"[Exa] Contents fetched for {len(results)} URLs")
            return results

        except Exception as e:
            logger.error(f"[Exa] Contents fetch failed: {e}")
            return []


class FirecrawlSearch:
    """Web scraping via Firecrawl API."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self.available = bool(api_key)
        self.base_url = "https://api.firecrawl.dev/v0"

    def search(self, query: str, limit: int = 8) -> list[dict]:
        """Search and get page content."""
        if not self.available:
            return []

        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {"query": query, "limit": limit}
            response = requests.post(
                f"{self.base_url}/search",
                json=payload,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            results = []
            for item in data.get("data", []):
                results.append({
                    "url": item.get("url", ""),
                    "title": item.get("metadata", {}).get("title", ""),
                    "text": item.get("markdown", "")[:5000],
                })

            logger.info(f"[Firecrawl] Search returned {len(results)} results")
            return results

        except Exception as e:
            logger.error(f"[Firecrawl] Search failed: {e}")
            return []

    def scrape(self, url: str) -> str:
        """Scrape a single URL and return markdown content."""
        if not self.available:
            return ""

        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {"url": url}
            response = requests.post(
                f"{self.base_url}/scrape",
                json=payload,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("data", {}).get("markdown", "")[:8000]

        except Exception as e:
            logger.error(f"[Firecrawl] Scrape failed for {url}: {e}")
            return ""
