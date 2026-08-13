# SQLite adapter for ApiKeyRepository port.
# Durable, cross-process API key storage — closes F12 (keys previously lived
# only in a process-local dict, so they didn't survive a restart and were
# invisible to any other worker process).

import logging
from datetime import datetime

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

    async def initialize(self):
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute(CREATE_API_KEYS_TABLE)
        for idx in CREATE_API_KEYS_INDEXES:
            await self._conn.execute(idx)
        await self._conn.commit()
        logger.info("API key repository initialized at %s", self.db_path)

    async def close(self):
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
    def _row_to_key(row) -> ApiKey:
        return ApiKey(
            key_id=row["key_id"],
            key_hash=row["key_hash"],
            tier=ApiTier(row["tier"]),
            owner_email=row["owner_email"],
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            is_active=bool(row["is_active"]),
        )
