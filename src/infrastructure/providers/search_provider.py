# Search provider port and adapters.
# Query-to-URL discovery: mirrors enrichment_provider.py's Strategy + Null Object shape.

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from src.domain.models import SearchHit
from src.infrastructure.cache import AICache

logger = logging.getLogger("Spacescraper.SearchProvider")


class SearchProvider(ABC):
    """Port for query-to-URL discovery (search engine adapters)."""

    @abstractmethod
    async def search(self, query: str, *, max_results: int = 10) -> list[SearchHit]:
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

    async def search(self, query: str, *, max_results: int = 10) -> list[SearchHit]:
        return []

    async def is_available(self) -> bool:
        return True


class DuckDuckGoSearchProvider(SearchProvider):
    """
    HTML-scraping DuckDuckGo adapter. No API key required, no new dependency
    (uses the existing guarded http_client + beautifulsoup4).
    """

    SEARCH_URL = "https://html.duckduckgo.com/html/"

    def __init__(self, cache: AICache | None = None):
        self.cache = cache or AICache(local_maxsize=200)

    async def _get_client(self):
        from src.infrastructure.http_client import HttpClient
        return await HttpClient.get_client()

    async def search(self, query: str, *, max_results: int = 10) -> list[SearchHit]:
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

    def _parse_results(self, html: str, max_results: int) -> list[SearchHit]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        hits: list[SearchHit] = []

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

    def __init__(self, api_key: str | None = None, cache: AICache | None = None):
        self.api_key = api_key
        self._enabled = bool(api_key)
        self.cache = cache or AICache(local_maxsize=200)

    async def _get_client(self):
        from src.infrastructure.http_client import HttpClient
        return await HttpClient.get_client()

    async def search(self, query: str, *, max_results: int = 10) -> list[SearchHit]:
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
            data = response.json()
        except Exception as e:
            logger.warning("Serper search failed for query: %s", e)
            return []

        hits: list[SearchHit] = []
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


class OpenRouterSearchProvider(SearchProvider):
    """
    OpenRouter server-tool web search.

    Uses the current server-tool syntax
    (`tools: [{"type": "openrouter:web_search", ...}]`); the older `plugins`
    form and the `:online` model suffix are deprecated and not used here.

    Two things make this adapter different from the Serper and DuckDuckGo ones,
    and both are deliberate:

    Cost. Every call is billed per request on top of tokens
    (WEB_SEARCH_PRICE_PER_REQUEST_USD), so `max_results` is clamped *before* the
    request is made rather than after. Discovery caps how many hits can ever
    become jobs (max_fanout); asking a billed API for more than that ceiling is
    spend that cannot change the outcome. Results are cached for the same
    reason — an identical repeated query must not re-bill.

    Trust. The other adapters read a real SERP. This one reads a model's
    response, and a model can invent a plausible URL. Only `url_citation`
    annotations are parsed — never URLs from the reply text — because
    annotations are the tool's own record of pages it actually fetched, whereas
    prose URLs are generated tokens. This adapter does not enforce the domain
    allowlist itself; DiscoveryService applies UrlPolicy and the SSRF guard to
    every hit returned here, and nothing below is permitted to bypass that.
    """

    def __init__(
        self,
        api_key: str | None = None,
        cache: AICache | None = None,
        max_fanout: int | None = None,
    ):
        from src.infrastructure.ai.ssot import WEB_SEARCH_CACHE_MAXSIZE

        self.api_key = api_key
        self._enabled = bool(api_key)
        self.cache = cache or AICache(local_maxsize=WEB_SEARCH_CACHE_MAXSIZE)
        # Discovery's fan-out budget, used as a spend ceiling on the request.
        self.max_fanout = max_fanout

        self.failure_count = 0
        self.offline_until = 0.0

    def _check_circuit(self) -> bool:
        if not self._enabled:
            return False
        return time.time() >= self.offline_until

    def _record_failure(self, error: Exception) -> None:
        from src.infrastructure.ai.ssot import RESILIENCE

        self.failure_count += 1
        logger.error(
            "OpenRouter search failure (%d/%d): %s",
            self.failure_count, RESILIENCE.breaker_threshold, error,
        )
        if self.failure_count >= RESILIENCE.breaker_threshold:
            self.offline_until = time.time() + RESILIENCE.cooldown_period_s
            logger.critical(
                "OpenRouter search CIRCUIT OPEN: offline for %ss",
                RESILIENCE.cooldown_period_s,
            )

    def _record_success(self) -> None:
        if self.failure_count > 0:
            logger.info("OpenRouter search CIRCUIT CLOSED: connection restored")
        self.failure_count = 0

    def _effective_max_results(self, requested: int) -> int:
        """Clamp the billed result count to what Discovery could actually use."""
        from src.infrastructure.ai.ssot import WEB_SEARCH_MAX_RESULTS_CAP

        limits = [WEB_SEARCH_MAX_RESULTS_CAP, requested]
        if self.max_fanout is not None:
            limits.append(self.max_fanout)
        return max(1, min(limits))

    async def _get_client(self) -> Any:
        from src.infrastructure.http_client import internal_http
        return internal_http

    async def search(self, query: str, *, max_results: int = 10) -> list[SearchHit]:
        from src.infrastructure.ai.ssot import (
            ENDPOINTS,
            RESILIENCE,
            WEB_SEARCH_TOOL_TYPE,
            AIJob,
            profile_for,
        )

        if not self._check_circuit():
            return []

        effective = self._effective_max_results(max_results)
        if effective < max_results:
            logger.info(
                "OpenRouter search: capping %d requested results to %d "
                "(fan-out budget); each request is separately billed.",
                max_results, effective,
            )

        cache_key = f"{query}|{effective}"
        cached = await self.cache.get("openrouter", "search", cache_key)
        if cached is not None:
            logger.debug("OpenRouter search: cache hit, no request billed")
            return [SearchHit(**h) for h in cached]

        profile = profile_for(AIJob.SEARCH)
        payload: dict[str, Any] = {
            "model": profile.model.id,
            "messages": [{"role": "user", "content": query[: profile.max_prompt_chars]}],
            "tools": [
                {
                    "type": WEB_SEARCH_TOOL_TYPE,
                    "parameters": {"max_results": effective},
                }
            ],
        }
        if len(profile.model_chain) > 1:
            payload["models"] = list(profile.model_chain)
        if profile.temperature_allowed:
            payload["temperature"] = profile.temperature

        client = await self._get_client()
        data: dict[str, Any] | None = None
        for attempt in range(RESILIENCE.max_retries):
            try:
                response = await client.post(
                    ENDPOINTS.openrouter_chat,
                    json=payload,
                    timeout=profile.timeout_s,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                data = response.json()
                self._record_success()
                break
            except Exception as e:  # noqa: BLE001 — port contract: never raise
                if attempt < RESILIENCE.max_retries - 1:
                    delay = RESILIENCE.base_delay_s * (2 ** attempt)
                    logger.warning(
                        "OpenRouter search attempt %d/%d failed: %s. Retrying in %ss...",
                        attempt + 1, RESILIENCE.max_retries, e, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    self._record_failure(e)
                    return []

        if data is None:
            return []

        hits = _parse_url_citations(data, effective)
        if not hits:
            logger.warning(
                "OpenRouter search returned no url_citation annotations for a billed "
                "request; the model may have answered without invoking the tool."
            )
            return []

        await self.cache.set(
            "openrouter", "search", cache_key, [h.model_dump() for h in hits]
        )
        return hits

    async def is_available(self) -> bool:
        return self._check_circuit()


def _parse_url_citations(data: dict[str, Any], max_results: int) -> list[SearchHit]:
    """Extract hits from `url_citation` annotations only.

    Deliberately ignores the reply text. A URL in prose is a generated token and
    may not exist; a url_citation records a page the search tool actually
    retrieved. Reading prose URLs would let a model inject arbitrary targets
    into the crawl queue.
    """
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return []

    annotations = message.get("annotations") or []
    hits: list[SearchHit] = []
    seen: set[str] = set()

    for annotation in annotations:
        if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
            continue
        # OpenRouter nests the payload under `url_citation`; tolerate a flat
        # shape too rather than silently dropping every hit if that changes.
        citation = annotation.get("url_citation")
        if not isinstance(citation, dict):
            citation = annotation

        url = citation.get("url")
        if not isinstance(url, str) or not url or url in seen:
            continue
        seen.add(url)

        hits.append(
            SearchHit(
                url=url,
                title=citation.get("title") or "",
                snippet=citation.get("content") or citation.get("snippet") or "",
                rank=len(hits),
                provider="openrouter",
            )
        )
        if len(hits) >= max_results:
            break

    return hits
