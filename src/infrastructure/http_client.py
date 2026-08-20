# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Shared Infrastructure)
# Role: Two HTTP client singletons split by trust direction (S2).
#
# internal_http talks to services we operate or trust (webhooks, the LLM
# API) — an honest, neutral UA with no bot signature or personal name.
# target_http talks to scrape targets — its UA is drawn from the same
# Fingerprint value object (S1) the browser tier uses, so the HTTP and
# browser paths present a coherent identity. Both are SSRF-guarded.

import asyncio
import random

import httpx

from src.domain.fingerprint import OS_PROFILES, build_fingerprint
from src.security.validating_transport import SSRFValidatingTransport

_DEFAULT_TARGET_CHROMIUM_MAJOR = 120


class _HttpClient:
    """Lazily-initialized, SSRF-guarded httpx.AsyncClient singleton."""

    def __init__(self, user_agent: str | None):
        self._user_agent = user_agent
        self._instance: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def get_client(self) -> httpx.AsyncClient:
        if self._instance is None or self._instance.is_closed:
            async with self._lock:
                if self._instance is None or self._instance.is_closed:
                    headers = {"User-Agent": self._user_agent} if self._user_agent else {}
                    self._instance = httpx.AsyncClient(
                        timeout=30.0,
                        follow_redirects=True,
                        headers=headers,
                        limits=httpx.Limits(
                            max_connections=100,
                            max_keepalive_connections=20,
                            keepalive_expiry=30.0,
                        ),
                        # Re-validates the destination at connection time and on
                        # every redirect hop, closing the DNS-rebinding and
                        # redirect-to-private-IP gaps in the submit-time-only
                        # check in ssrf_guard.py (F13).
                        transport=SSRFValidatingTransport(),
                    )
        return self._instance

    async def close(self):
        async with self._lock:
            if self._instance and not self._instance.is_closed:
                await self._instance.aclose()
                self._instance = None

    async def post(self, url: str, **kwargs):
        client = await self.get_client()
        return await client.post(url, **kwargs)

    async def get(self, url: str, **kwargs):
        client = await self.get_client()
        return await client.get(url, **kwargs)

    async def head(self, url: str, **kwargs):
        client = await self.get_client()
        return await client.head(url, **kwargs)


# No custom UA: httpx's own default ("python-httpx/x.y.z") is already an
# honest, neutral identification for calls to services we operate or trust.
internal_http = _HttpClient(user_agent=None)

# A real, coherent browser UA (S1's Fingerprint) — never a literal, never a
# self-declaring bot string. Fixed seed: this singleton isn't tied to a job
# or persona, just needs an internally-consistent UA for the pre-P1 httpx tier.
target_http = _HttpClient(
    user_agent=build_fingerprint(
        _DEFAULT_TARGET_CHROMIUM_MAJOR, OS_PROFILES[0], random.Random("target-http-client")
    ).user_agent
)
