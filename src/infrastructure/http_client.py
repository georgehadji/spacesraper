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
import logging
import random

import httpx

from src.domain.fingerprint import OS_PROFILES, build_fingerprint
from src.security.ssrf_guard import resolve_and_validate_hostname, validate_outbound_url
from src.security.validating_transport import SSRFValidatingTransport

logger = logging.getLogger("Spacescraper.HttpClient")

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


# ---------------------------------------------------------------------------
# Second client surface, from the discovery branch.
#
# Both surfaces are kept because both have live callers: the _HttpClient
# singletons above (internal_http/target_http) are used by sitemap_seeder,
# notifier, robots, image_downloader, smart_crawler and the Gemini path,
# while HttpClient/http_client/create_scoped_client below are used by
# search_provider, url_policy and LocalLLMProvider. Dropping either one
# would break its callers with an ImportError at startup — and those caller
# files merge cleanly, so nothing would flag it.
#
# They guard the same way by different means: _HttpClient wraps
# SSRFValidatingTransport, HttpClient wraps GuardedTransport. Consolidating
# onto one transport is worthwhile, but is a refactor with its own test
# surface, not part of this merge.
# ---------------------------------------------------------------------------

class GuardedTransport(httpx.AsyncBaseTransport):
    """
    Enforces the SSRF policy on every request, including redirect hops.
    Wraps the underlying httpx transport and validates each request URL.

    allow_private=True disables the guard entirely — tests only, never use
    on a client shared across the process (see HttpClient.get_client, which
    always guards). allowed_private_hosts is the production-safe alternative:
    it exempts only the exact configured hostnames (e.g. a local LLM endpoint)
    from the private-IP check while every other request, including redirects
    to any other host, is still fully validated.
    """
    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        allow_private: bool = False,
        allowed_private_hosts: frozenset[str] | None = None,
    ):
        self._inner = inner
        self._allow_private = allow_private
        self._allowed_private_hosts = allowed_private_hosts or frozenset()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if not self._allow_private and host not in self._allowed_private_hosts:
            try:
                validate_outbound_url(str(request.url))
                _, pinned_ips = resolve_and_validate_hostname(host)
            except Exception as e:
                logger.error(f"SSRF guard rejected request to {request.url}: {e}")
                raise
            # Pin the connection to the IP just validated. Without this the
            # inner transport re-resolves DNS independently at connect time,
            # reopening the DNS-rebinding TOCTOU window this guard exists to
            # close. Host header + SNI keep the original hostname so virtual
            # hosting and TLS certificate validation are unaffected.
            request.headers["Host"] = host
            request.extensions["sni_hostname"] = host
            request.url = request.url.copy_with(host=pinned_ips[0])
        return await self._inner.handle_async_request(request)


class HttpClient:
    """
    Spacescraper Networking Node.
    Ensures that the entire application reuses a single connection pool 
    via the Singleton pattern. This prevents socket exhaustion and 
    optimizes network performance for high-frequency requests.
    """
    _instance: httpx.AsyncClient | None = None
    _lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    async def get_client(cls, *, allow_private: bool = False) -> httpx.AsyncClient:
        """
        Retrieves or initializes the shared httpx.AsyncClient.
        Configured with enterprise defaults for timeouts and redirection.
        Thread-safe singleton implementation.
        Wraps transport with SSRF guard unless allow_private=True (tests only).
        """
        if cls._instance is None or cls._instance.is_closed:
            async with cls._lock:
                # Double-check pattern to prevent race conditions
                if cls._instance is None or cls._instance.is_closed:
                    # Create guarded transport that validates every request + redirect
                    base_transport = httpx.AsyncHTTPTransport(
                        limits=httpx.Limits(
                            max_connections=100,
                            max_keepalive_connections=20,
                            keepalive_expiry=30.0
                        )
                    )
                    guarded = GuardedTransport(base_transport, allow_private=allow_private)

                    cls._instance = httpx.AsyncClient(
                        transport=guarded,
                        timeout=30.0,
                        follow_redirects=True,
                        headers={"User-Agent": "Spacescraper/2.4 (Enterprise Pipeline; Georgios-Chrysovalantis Chatzivantsidis)"},
                    )
        return cls._instance

    @classmethod
    async def close(cls):
        """Cleanly releases the connection pool."""
        async with cls._lock:
            if cls._instance and not cls._instance.is_closed:
                await cls._instance.aclose()
                cls._instance = None

    @classmethod
    async def post(cls, url: str, **kwargs):
        """Convenience method for POST requests using the singleton client."""
        client = await cls.get_client()
        return await client.post(url, **kwargs)

    @classmethod
    async def get(cls, url: str, **kwargs):
        """Convenience method for GET requests using the singleton client."""
        client = await cls.get_client()
        return await client.get(url, **kwargs)

    @classmethod
    async def head(cls, url: str, **kwargs):
        """Convenience method for HEAD requests using the singleton client."""
        client = await cls.get_client()
        return await client.head(url, **kwargs)

def create_scoped_client(
    *,
    allowed_private_hosts: set[str] | None = None,
    timeout: float = 30.0,
) -> httpx.AsyncClient:
    """
    Creates a standalone, non-singleton guarded httpx.AsyncClient that exempts
    only `allowed_private_hosts` from the private-IP check — never the shared
    HttpClient.get_client() singleton, so this exemption cannot leak into
    unrelated requests elsewhere in the process.

    Intended for adapters that must legitimately reach a private-address
    endpoint (e.g. a local LLM server) without weakening the SSRF guard for
    everything else. Caller owns the client's lifecycle (aclose() it).
    """
    base_transport = httpx.AsyncHTTPTransport()
    guarded = GuardedTransport(
        base_transport,
        allow_private=False,
        allowed_private_hosts=frozenset(allowed_private_hosts or ()),
    )
    return httpx.AsyncClient(transport=guarded, timeout=timeout, follow_redirects=True)


# Global singleton instance for use across the ecosystem
http_client = HttpClient()
