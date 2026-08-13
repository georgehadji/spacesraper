# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Persistence & Intelligence)
# Role: SQLite-backed repository for tracking extracted-record lifecycles.

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import aiosqlite

from src.domain.models import ExtractedRecord

logger = logging.getLogger("Spacescraper.SqliteTracker")

class SqliteTracker:
    """
    Spacescraper State Auditor.
    Maintains a persistent record of all discovered entities to enable
    accurate change detection across scrapes.
    Uses connection pooling for better performance.
    """

    def __init__(self, db_path: str = "spacescraper_intel.db", pool_size: int = 5):
        self.db_path = db_path
        self._pool: list[aiosqlite.Connection] = []
        self._pool_size = pool_size
        self._lock_pool = []
        self._initialized = False

    async def initialize(self):
        """Provision the intelligence schema and connection pool."""
        if self._initialized:
            return

        # Create initial connection to set up schema
        async with aiosqlite.connect(self.db_path) as db:
            # Performance: WAL mode allows concurrent reads during a write operation
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute("PRAGMA temp_store=MEMORY")
            await db.execute("PRAGMA mmap_size=30000000")  # 30MB memory map

            await db.execute("""
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY,
                    record_type TEXT,
                    canonical_url TEXT,
                    source_url TEXT,
                    data TEXT,
                    identity_hash TEXT,
                    content_hash TEXT,
                    first_seen TIMESTAMP,
                    last_seen TIMESTAMP,
                    change_type TEXT,
                    data_classification TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    timestamp TIMESTAMP,
                    source TEXT,
                    new_count INTEGER,
                    updated_count INTEGER,
                    total_count INTEGER
                )
            """)

            # Optimization: Strategic indexes to speed up lookup and grouping
            await db.execute("CREATE INDEX IF NOT EXISTS idx_records_last_seen ON records(last_seen)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_records_type ON records(record_type)")

            await db.commit()

        # Initialize connection pool
        for _ in range(self._pool_size):
            conn = await aiosqlite.connect(self.db_path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            self._pool.append(conn)

        self._initialized = True
        logger.info(f"Spacescraper Intelligence DB initialized at {self.db_path} (pool: {self._pool_size})")

    @asynccontextmanager
    async def _get_connection(self):
        """Get a connection from the pool."""
        if not self._pool:
            # Fallback: create temporary connection if pool exhausted
            conn = await aiosqlite.connect(self.db_path)
            conn.row_factory = aiosqlite.Row
            try:
                yield conn
            finally:
                await conn.close()
        else:
            conn = self._pool.pop()
            try:
                yield conn
            finally:
                self._pool.append(conn)

    async def get_record_by_id(self, record_key: str) -> dict[str, Any] | None:
        """Retrieves a specific record snapshot for comparison, keyed by canonical/source URL."""
        async with self._get_connection() as db:
            async with db.execute("SELECT * FROM records WHERE id = ?", (record_key,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def upsert_record(self, record: ExtractedRecord) -> bool:
        """
        Persists or updates a record's tracked state.
        Keyed by canonical_url (falling back to source_url) — same identity
        Deduplicator and IntelligencePostProcessor use, so the same real-world
        entity tracks consistently across scrapes regardless of record_id.
        Returns True if inserted (new), False if updated.
        """
        record_key = record.canonical_url or record.source_url

        async with self._get_connection() as db:
            # Check if exists
            async with db.execute("SELECT 1 FROM records WHERE id = ?", (record_key,)) as cursor:
                exists = await cursor.fetchone() is not None

            data_json = json.dumps(record.data, default=str)

            await db.execute("""
                INSERT INTO records (
                    id, record_type, canonical_url, source_url, data,
                    identity_hash, content_hash, first_seen, last_seen,
                    change_type, data_classification
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    record_type = excluded.record_type,
                    data = excluded.data,
                    identity_hash = excluded.identity_hash,
                    content_hash = excluded.content_hash,
                    last_seen = excluded.last_seen,
                    change_type = excluded.change_type,
                    data_classification = excluded.data_classification
            """, (
                record_key, record.record_type, record.canonical_url, record.source_url,
                data_json, record.identity_hash, record.content_hash,
                record.first_seen.isoformat(), record.last_seen.isoformat(),
                record.change_type.value, record.data_classification,
            ))
            await db.commit()
            return not exists

    async def log_run(self, run_id: str, source: str, counts: dict[str, int]):
        """Records a scraper execution session."""
        async with self._get_connection() as db:
            await db.execute("""
                INSERT INTO runs (run_id, timestamp, source, new_count, updated_count, total_count)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                run_id, datetime.now().isoformat(), source,
                counts.get('NEW', 0), counts.get('UPDATED', 0), counts.get('TOTAL', 0)
            ))
            await db.commit()

    async def close(self):
        """Close all pooled connections."""
        for conn in self._pool:
            try:
                await conn.close()
            except Exception:
                pass
        self._pool.clear()
        self._initialized = False

# Global tracker instance
intel_tracker = SqliteTracker()
