"""
Valkey-backed API key store.
Persists hashed keys and metadata so they survive process restarts and are shared across replicas.
"""

import json
import logging
from typing import Optional, Dict, Any

import valkey.asyncio as valkey

logger = logging.getLogger("Spacescraper.ApiKeyStore")

# Redis key prefix for API keys
_APIKEY_PREFIX = "apikey:"


class ValkeyApiKeyStore:
    """Implements ApiKeyStore protocol using Valkey (distributed Redis)."""

    def __init__(self, redis: valkey.Redis):
        self._redis = redis

    async def save(self, key_hash: str, key_data: Dict[str, Any]) -> None:
        """
        Save a hashed API key with metadata.
        Store as JSON string to preserve type information.
        """
        redis_key = f"{_APIKEY_PREFIX}{key_hash}"
        # Serialize metadata: ensure datetime objects are converted to ISO strings
        if "created_at" in key_data and hasattr(key_data["created_at"], "isoformat"):
            key_data = dict(key_data)  # Shallow copy to avoid modifying caller's dict
            key_data["created_at"] = key_data["created_at"].isoformat()
        if "expires_at" in key_data and key_data["expires_at"] and hasattr(key_data["expires_at"], "isoformat"):
            key_data["expires_at"] = key_data["expires_at"].isoformat()

        await self._redis.set(redis_key, json.dumps(key_data))
        logger.debug(f"Saved API key {key_hash[:8]}...")

    async def get_by_hash(self, key_hash: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve API key data by its hash.
        Returns None if key not found or revoked (is_active=False).
        """
        redis_key = f"{_APIKEY_PREFIX}{key_hash}"
        data = await self._redis.get(redis_key)

        if not data:
            return None

        key_data = json.loads(data)

        # Revoked keys are denied (is_active=False)
        if not key_data.get("is_active", True):
            logger.debug(f"Key {key_hash[:8]}... is revoked")
            return None

        return key_data

    async def revoke(self, key_hash: str) -> None:
        """
        Mark an API key as revoked by setting is_active=False.
        Does not delete the key, preserving audit history.
        """
        redis_key = f"{_APIKEY_PREFIX}{key_hash}"
        data = await self._redis.get(redis_key)

        if not data:
            logger.warning(f"Cannot revoke unknown key {key_hash[:8]}...")
            return

        key_data = json.loads(data)
        key_data["is_active"] = False

        await self._redis.set(redis_key, json.dumps(key_data))
        logger.info(f"Revoked API key {key_hash[:8]}...")
