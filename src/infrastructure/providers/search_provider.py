# Search provider port and adapters.
# Query-to-URL discovery: mirrors enrichment_provider.py's Strategy + Null Object shape.

import logging
from typing import List, Optional
from abc import ABC, abstractmethod

from src.domain.models import SearchHit
from src.infrastructure.cache import AICache

logger = logging.getLogger("Spacescraper.SearchProvider")


class SearchProvider(ABC):
    """Port for query-to-URL discovery (search engine adapters)."""

    @abstractmethod
    async def search(self, query: str, *, max_results: int = 10) -> List[SearchHit]:
        """Execute a search query and return ranked hits. Never raises on failure."""
        ...

    @abstractmethod
    async def is_available(self) -> bool:
        """Check if the provider is configured and reachable."""
        ...


class NoOpSearchProvider(SearchProvider):
    """
    Default adapter. Returns no results.
    Keeps Discovery dark until both features["discovery"]=True AND a real
    provider are configured — no `if enabled:` branches needed at call sites.
    """

    async def search(self, query: str, *, max_results: int = 10) -> List[SearchHit]:
        return []

    async def is_available(self) -> bool:
        return True


class DuckDuckGoSearchProvider(SearchProvider):
    """
    HTML-scraping DuckDuckGo adapter. No API key required, no new dependency
    (uses the existing guarded http_client + beautifulsoup4).
    """

    SEARCH_URL = "https://html.duckduckgo.com/html/"

    def __init__(self, cache: Optional[AICache] = None):
        self.cache = cache or AICache(local_maxsize=200)

    async def _get_client(self):
        from src.infrastructure.http_client import HttpClient
        return await HttpClient.get_client()

    async def search(self, query: str, *, max_results: int = 10) -> List[SearchHit]:
        cache_key = f"{query}|{max_results}"
        cached = await self.cache.get("duckduckgo", "html", cache_key)
        if cached is not None:
            return [SearchHit(**h) for h in cached]

        try:
            client = await self._get_client()
            response = await client.post(
                self.SEARCH_URL, data={"q": query}, timeout=10.0
            )
            hits = self._parse_results(response.text, max_results)
        except Exception as e:
            logger.warning("DuckDuckGo search failed for query: %s", e)
            return []

        if hits:
            await self.cache.set(
                "duckduckgo", "html", cache_key, [h.model_dump() for h in hits]
            )
        return hits

    def _parse_results(self, html: str, max_results: int) -> List[SearchHit]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        hits: List[SearchHit] = []

        for rank, result in enumerate(soup.select(".result")):
            if len(hits) >= max_results:
                break
            link = result.select_one(".result__a")
            snippet_el = result.select_one(".result__snippet")
            if not link or not link.get("href"):
                continue

            hits.append(
                SearchHit(
                    url=link["href"],
                    title=link.get_text(strip=True),
                    snippet=snippet_el.get_text(strip=True) if snippet_el else "",
                    rank=rank,
                    provider="duckduckgo",
                )
            )

        return hits

    async def is_available(self) -> bool:
        return True


class SerperSearchProvider(SearchProvider):
    """Serper.dev Google Search API adapter. Key-gated."""

    SEARCH_URL = "https://google.serper.dev/search"

    def __init__(self, api_key: Optional[str] = None, cache: Optional[AICache] = None):
        self.api_key = api_key
        self._enabled = bool(api_key)
        self.cache = cache or AICache(local_maxsize=200)

    async def _get_client(self):
        from src.infrastructure.http_client import HttpClient
        return await HttpClient.get_client()

    async def search(self, query: str, *, max_results: int = 10) -> List[SearchHit]:
        if not self._enabled:
            return []

        cache_key = f"{query}|{max_results}"
        cached = await self.cache.get("serper", "search", cache_key)
        if cached is not None:
            return [SearchHit(**h) for h in cached]

        try:
            client = await self._get_client()
            response = await client.post(
                self.SEARCH_URL,
                json={"q": query, "num": max_results},
                headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
                timeout=10.0,
            )
            data = await response.json()
        except Exception as e:
            logger.warning("Serper search failed for query: %s", e)
            return []

        hits: List[SearchHit] = []
        for rank, item in enumerate(data.get("organic", [])[:max_results]):
            url = item.get("link")
            if not url:
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=item.get("title", ""),
                    snippet=item.get("snippet", ""),
                    rank=rank,
                    provider="serper",
                )
            )

        if hits:
            await self.cache.set(
                "serper", "search", cache_key, [h.model_dump() for h in hits]
            )
        return hits

    async def is_available(self) -> bool:
        return self._enabled
