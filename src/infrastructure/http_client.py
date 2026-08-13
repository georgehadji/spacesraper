# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Shared Infrastructure)
# Role: Provides a centralized, high-performance async HTTP client singleton.

import asyncio

import httpx

from src.security.validating_transport import SSRFValidatingTransport


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
    async def get_client(cls) -> httpx.AsyncClient:
        """
        Retrieves or initializes the shared httpx.AsyncClient.
        Configured with enterprise defaults for timeouts and redirection.
        Thread-safe singleton implementation.
        """
        if cls._instance is None or cls._instance.is_closed:
            async with cls._lock:
                # Double-check pattern to prevent race conditions
                if cls._instance is None or cls._instance.is_closed:
                    # Configure standard headers for industrial identification
                    # User-Agent identifies the bot according to best practices
                    cls._instance = httpx.AsyncClient(
                        timeout=30.0,
                        follow_redirects=True,
                        headers={"User-Agent": "Spacescraper/2.4 (Enterprise Pipeline; Georgios-Chrysovalantis Chatzivantsidis)"},
                        limits=httpx.Limits(
                            max_connections=100,
                            max_keepalive_connections=20,
                            keepalive_expiry=30.0
                        ),
                        # Re-validates the destination at connection time and on
                        # every redirect hop, closing the DNS-rebinding and
                        # redirect-to-private-IP gaps in the submit-time-only
                        # check in ssrf_guard.py (F13).
                        transport=SSRFValidatingTransport(),
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
