# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Shared Infrastructure)
# Role: Provides a centralized, high-performance async HTTP client singleton.

import httpx
import asyncio
from typing import Optional
import logging

from src.security.ssrf_guard import validate_outbound_url

logger = logging.getLogger("Spacescraper.HttpClient")


class GuardedTransport(httpx.AsyncBaseTransport):
    """
    Enforces the SSRF policy on every request, including redirect hops.
    Wraps the underlying httpx transport and validates each request URL.
    """
    def __init__(self, inner: httpx.AsyncBaseTransport, *, allow_private: bool = False):
        self._inner = inner
        self._allow_private = allow_private

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self._allow_private:
            try:
                validate_outbound_url(str(request.url))
            except Exception as e:
                logger.error(f"SSRF guard rejected request to {request.url}: {e}")
                raise
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

# Global singleton instance for use across the ecosystem
http_client = HttpClient()
