# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Shared Infrastructure)
# Role: Provides a centralized, high-performance async HTTP client singleton.

import httpx
import asyncio
from typing import Optional, Set, FrozenSet
import logging

from src.security.ssrf_guard import validate_outbound_url, resolve_and_validate_hostname

logger = logging.getLogger("Spacescraper.HttpClient")


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
        allowed_private_hosts: Optional[FrozenSet[str]] = None,
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
    _instance: Optional[httpx.AsyncClient] = None
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
    allowed_private_hosts: Optional[Set[str]] = None,
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
