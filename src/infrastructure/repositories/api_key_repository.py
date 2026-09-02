# Two API key stores live here, for two different callers:
#
# SqliteApiKeyRepository implements the ApiKeyRepository port and is what
# auth_middleware.py's ApiKeyManager uses. It is the only one of the two
# that supports revocation end to end (get_by_key_id + set_active), which
# ApiKeyManager.revoke_key and the `revoke` CLI command depend on.
#
# ValkeyApiKeyStore is the Valkey-backed store from the discovery branch,
# kept because its own test suite covers it. It has no get_by_key_id, so a
# leaked key can't be revoked through it by the key_id an operator holds —
# don't wire it into ApiKeyManager without closing that gap first.
#
# Both closed F12 (keys previously lived in a process-local dict: lost on
# restart, invisible to other workers).

# SQLite adapter for ApiKeyRepository port.
# Durable, cross-process API key storage — closes F12 (keys previously lived
# only in a process-local dict, so they didn't survive a restart and were
# invisible to any other worker process).

import logging
from datetime import datetime
from typing import Any

import aiosqlite

from src.domain.models import ApiKey, ApiTier

logger = logging.getLogger("Spacescraper.ApiKeyRepository")

CREATE_API_KEYS_TABLE = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash TEXT PRIMARY KEY,
    key_id TEXT NOT NULL UNIQUE,
    tier TEXT NOT NULL,
    owner_email TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1
)
"""

CREATE_API_KEYS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_api_keys_key_id ON api_keys(key_id)",
]


class SqliteApiKeyRepository:
    """SQLite-backed implementation of ApiKeyRepository."""

    def __init__(self, db_path: str = "spacescraper_jobs.db"):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute(CREATE_API_KEYS_TABLE)
        for idx in CREATE_API_KEYS_INDEXES:
            await self._conn.execute(idx)
        await self._conn.commit()
        logger.info("API key repository initialized at %s", self.db_path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def create_key(self, key: ApiKey) -> ApiKey:
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO api_keys
               (key_hash, key_id, tier, owner_email, created_at, expires_at, is_active)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                key.key_hash, key.key_id, key.tier.value, key.owner_email,
                key.created_at.isoformat(),
                key.expires_at.isoformat() if key.expires_at else None,
                int(key.is_active),
            ),
        )
        await self._conn.commit()
        return key

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,)
        ) as cursor:
            row = await cursor.fetchone()
            return self._row_to_key(row) if row else None

    async def get_by_key_id(self, key_id: str) -> ApiKey | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM api_keys WHERE key_id = ?", (key_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return self._row_to_key(row) if row else None

    async def set_active(self, key_hash: str, is_active: bool) -> ApiKey | None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE api_keys SET is_active = ? WHERE key_hash = ?",
            (int(is_active), key_hash),
        )
        await self._conn.commit()
        return await self.get_by_hash(key_hash)

    @staticmethod
    def _row_to_key(row: Any) -> ApiKey:
        return ApiKey(
            key_id=row["key_id"],
            key_hash=row["key_hash"],
            tier=ApiTier(row["tier"]),
            owner_email=row["owner_email"],
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            is_active=bool(row["is_active"]),
        )


"""
Valkey-backed API key store.
Persists hashed keys and metadata so they survive process restarts and are shared across replicas.
"""

import json
import logging
from typing import Any

import valkey.asyncio as valkey

logger = logging.getLogger("Spacescraper.ApiKeyStore")

# Redis key prefix for API keys
_APIKEY_PREFIX = "apikey:"


class ValkeyApiKeyStore:
    """Implements ApiKeyStore protocol using Valkey (distributed Redis)."""

    def __init__(self, redis: valkey.Redis):
        self._redis = redis

    async def save(self, key_hash: str, key_data: dict[str, Any]) -> None:
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

    async def get_by_hash(self, key_hash: str) -> dict[str, Any] | None:
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

        # json.loads returns Any; the signature promises a concrete dict.
        return dict(key_data)

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
