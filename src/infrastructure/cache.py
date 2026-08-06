# Two-level cache for AI enrichment results.
# Local LRU cache (memory) + Valkey cache (distributed).

import json
import hashlib
import logging
from typing import Optional, Dict, Any
from collections import OrderedDict

logger = logging.getLogger("Spacescraper.AICache")


class LocalLRUCache:
    """Thread-safe local LRU cache with max size."""

    def __init__(self, maxsize: int = 1000):
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._maxsize = maxsize

    def get(self, key: str) -> Optional[Any]:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def set(self, key: str, value: Any):
        self._cache[key] = value
        self._cache.move_to_end(key)
        if len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)

    def clear(self):
        self._cache.clear()

    @property
    def size(self) -> int:
        return len(self._cache)


class AICache:
    """
    Two-level cache for AI enrichment results.
    Level 1: Local LRU (fast, per-process)
    Level 2: Valkey (shared across workers)
    Keyed by provider + model + content_hash.
    """

    def __init__(self, local_maxsize: int = 1000, valkey_client=None,
                 use_valkey: bool = True):
        self.local = LocalLRUCache(maxsize=local_maxsize)
        self._valkey = valkey_client
        self._use_valkey = use_valkey
        # Connect lazily: AICache is built at import time, so creating the
        # client here would bind it to the wrong (or no) event loop.
        self._connect_attempted = valkey_client is not None
        self._prefix = "ai_cache:"

    async def _get_valkey(self):
        """Return the L2 client, connecting on first use. None if unavailable."""
        if self._valkey is not None:
            return self._valkey
        if not self._use_valkey or self._connect_attempted:
            return None
        self._connect_attempted = True
        try:
            import valkey.asyncio as valkey
            from src.config_settings import settings
            self._valkey = valkey.from_url(str(settings.valkey.url), decode_responses=True)
            logger.debug("AICache: L2 (Valkey) attached.")
        except Exception as e:
            logger.debug("AICache: L2 unavailable, running local-only: %s", e)
        return self._valkey

    def _make_key(self, provider: str, model: str, content: str) -> str:
        """Generate a cache key from provider, model, and content."""
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        return f"{provider}:{model}:{content_hash}"

    async def get(self, provider: str, model: str, content: str) -> Optional[Any]:
        """
        Get cached result. Checks local LRU first, then Valkey.
        """
        key = self._make_key(provider, model, content)

        # Level 1: Local LRU
        local_result = self.local.get(key)
        if local_result is not None:
            logger.debug("AICache: Local hit for %s", key)
            return local_result

        # Level 2: Valkey
        client = await self._get_valkey()
        if client:
            try:
                valkey_result = await client.get(self._prefix + key)
                if valkey_result:
                    parsed = json.loads(valkey_result)
                    # Promote to local cache
                    self.local.set(key, parsed)
                    logger.debug("AICache: Valkey hit for %s", key)
                    return parsed
            except Exception as e:
                logger.debug("AICache: Valkey error: %s", e)

        return None

    async def set(self, provider: str, model: str, content: str, value: Any,
                  ttl_seconds: int = 3600):
        """
        Store a cached result in both local and Valkey caches.
        """
        key = self._make_key(provider, model, content)

        # Level 1: Local LRU
        self.local.set(key, value)

        # Level 2: Valkey
        client = await self._get_valkey()
        if client:
            try:
                serialized = json.dumps(value, default=str)
                await client.setex(self._prefix + key, ttl_seconds, serialized)
            except Exception as e:
                logger.debug("AICache: Valkey store error: %s", e)

    async def clear(self):
        """Clear local cache. Valkey entries expire via TTL."""
        self.local.clear()

    @property
    def local_size(self) -> int:
        return self.local.size
