# P1 Tier 1: cheap HTTP fetch with a real browser TLS/JA3 fingerprint
# (curl_cffi impersonation) instead of paying Chromium startup+render cost
# for every page. docs/plans/2026-08-13-capability-enhancement-plan.md P1.

import logging
import time

from curl_cffi.requests import AsyncSession

from src.domain.block_signal import detect_block
from src.domain.exceptions import SSRFGuardError
from src.domain.fetch import FetchRequest, FetchResult
from src.domain.throttle import parse_retry_after
from src.security.ssrf_guard import validate_outbound_url

logger = logging.getLogger("Spacescraper.HttpFetcher")


class ImpersonatingHttpFetcher:
    """FetcherPort Tier 1 adapter.

    SSRF (R2): validated pre-flight via the same synchronous DNS-resolution
    gate validate_outbound_url() uses for webhooks. curl_cffi has no
    transport-hook mechanism to bind the resolved IP the way
    validating_transport.py's httpx transport does for Tier 2, so this tier
    carries a small residual DNS-rebinding window between the check and
    curl's own connect — acceptable for a cheap, escalate-on-any-doubt tier.
    """

    def __init__(self, impersonate: str = "chrome"):
        self.impersonate = impersonate

    async def fetch(self, request: FetchRequest) -> FetchResult:
        try:
            validate_outbound_url(request.url)
        except SSRFGuardError as e:
            return FetchResult(url=request.url, tier_used="http", error=f"SSRF blocked: {e}")

        started = time.monotonic()
        try:
            async with AsyncSession() as session:
                response = await session.get(
                    request.url, impersonate=self.impersonate, timeout=request.timeout_s,
                )
        except Exception as e:
            logger.debug("Tier-1 fetch failed for %s: %s", request.url, e)
            return FetchResult(
                url=request.url, tier_used="http", error=str(e),
                latency_ms=(time.monotonic() - started) * 1000,
            )

        latency_ms = (time.monotonic() - started) * 1000
        retry_after_s = None
        if response.status_code in (429, 503):
            retry_after = response.headers.get("retry-after")
            if retry_after:
                retry_after_s = parse_retry_after(retry_after)

        signal = detect_block(status_code=response.status_code, body_sample=response.text[:4000])
        return FetchResult(
            url=request.url, status_code=response.status_code, html=response.text,
            tier_used="http", blocked=signal.blocked, block_reason=signal.reason,
            latency_ms=latency_ms, retry_after_s=retry_after_s,
        )
