# P1 Tier 2: full Playwright stealth browser fetch, generic FetcherPort
# adapter for callers that only need HTML back (contract tests, future P2
# SitemapSeeder / P5 /scrape verb). worker_scraper.py's own hot path keeps
# using ScraperEngine directly instead of this wrapper — it needs the richer
# RawScrapePayload (intercepted JSON endpoints, persona) that a generic
# FetchResult deliberately doesn't carry.
# docs/plans/2026-08-13-capability-enhancement-plan.md P1.

from src.domain.fetch import FetchRequest, FetchResult
from src.infrastructure.browser.engine import ScraperEngine
from src.infrastructure.browser.pool import BrowserContextPool


class StealthBrowserFetcher:
    """FetcherPort Tier 2 adapter."""

    def __init__(self, context_pool: BrowserContextPool, persona_id: str | None = None):
        self.context_pool = context_pool
        self.persona_id = persona_id

    async def fetch(self, request: FetchRequest) -> FetchResult:
        engine = ScraperEngine(context_pool=self.context_pool)
        try:
            await engine.start(persona_id=self.persona_id)
            payload = await engine.crawl(request.url)
        finally:
            await engine.close()

        blocked = bool(payload.error_message) and "challenge detected" in (payload.error_message or "").lower()
        return FetchResult(
            url=request.url, status_code=payload.status_code, html=payload.html_content or "",
            tier_used="browser", blocked=blocked,
            block_reason=payload.error_message if blocked else None,
            retry_after_s=payload.retry_after_s,
            error=payload.error_message if not blocked else None,
        )
