# Postgres adapter for OutboxRepository port (C8/W5.3, R-W1).
# Mirrors SqliteOutboxRepository — see postgres_job_repository.py's module
# docstring for the pool-backed design.

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from src.config_settings import settings
from src.domain.models import OutboxEvent, OutboxStatus
from src.infrastructure.repositories.postgres_conn import PostgresConnection, asyncpg_dsn, create_pool_with_retry

logger = logging.getLogger("Spacescraper.PostgresOutboxRepository")

CREATE_OUTBOX_TABLE = """
CREATE TABLE IF NOT EXISTS outbox_events (
    event_id TEXT PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PENDING',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 10,
    last_error TEXT,
    last_attempt_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL
)
"""

CREATE_OUTBOX_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox_events(status)",
    "CREATE INDEX IF NOT EXISTS idx_outbox_created ON outbox_events(created_at)",
]


class PostgresOutboxRepository:
    """Postgres-backed implementation of OutboxRepository. Mirrors SqliteOutboxRepository."""

    def __init__(self, dsn: str):
        self.dsn = asyncpg_dsn(dsn)
        self._pool: asyncpg.Pool | None = None
        self._conn: PostgresConnection | None = None

    async def initialize(self) -> None:
        self._pool = await create_pool_with_retry(
            self.dsn, min_size=2, max_size=settings.database.pool_size + settings.database.max_overflow,
        )
        self._conn = PostgresConnection(self._pool)
        await self._conn.execute(CREATE_OUTBOX_TABLE)
        for idx in CREATE_OUTBOX_INDEXES:
            await self._conn.execute(idx)
        logger.info("Outbox repository initialized at Postgres")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            self._conn = None

    async def create_event(self, event: OutboxEvent, *, conn: Any = None) -> OutboxEvent:
        """Persist a new outbox event.

        conn, when given (the value yielded by JobRepository.transaction()),
        writes this insert on that held connection instead of self._conn's
        per-call pool acquisition — main.py's job-submission unit of work.
        """
        target = conn if conn is not None else self._conn
        assert target is not None
        await target.execute(
            """INSERT INTO outbox_events
               (event_id, aggregate_type, aggregate_id, event_type, payload,
                status, retry_count, max_retries, last_error, last_attempt_at, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
            event.event_id, event.aggregate_type, event.aggregate_id, event.event_type,
            json.dumps(event.payload, default=str),
            event.status.value, event.retry_count, event.max_retries,
            event.last_error, None, event.created_at,
        )
        return event

    async def get_pending_events(
        self, limit: int = 50, min_retry_delay_seconds: int = 10
    ) -> list[OutboxEvent]:
        assert self._conn is not None
        cutoff = datetime.now(UTC) - timedelta(seconds=min_retry_delay_seconds)
        rows = await self._conn.fetch(
            """SELECT * FROM outbox_events
               WHERE status = 'PENDING'
               AND (last_attempt_at IS NULL OR last_attempt_at < $1)
               ORDER BY created_at ASC LIMIT $2""",
            cutoff, limit,
        )
        return [self._row_to_event(r) for r in rows]

    async def mark_delivered(self, event_id: str) -> None:
        assert self._conn is not None
        now = datetime.now(UTC)
        await self._conn.execute(
            "UPDATE outbox_events SET status = 'DELIVERED', last_attempt_at = $1 WHERE event_id = $2",
            now, event_id,
        )

    async def mark_failed(self, event_id: str, error: str) -> None:
        assert self._conn is not None
        now = datetime.now(UTC)
        row = await self._conn.fetchrow(
            "SELECT retry_count, max_retries FROM outbox_events WHERE event_id = $1", event_id,
        )
        if not row:
            return
        retry_count = row["retry_count"] + 1
        max_retries = row["max_retries"]
        new_status = "FAILED" if retry_count >= max_retries else "PENDING"

        await self._conn.execute(
            """UPDATE outbox_events
               SET status = $1, retry_count = $2, last_error = $3, last_attempt_at = $4
               WHERE event_id = $5""",
            new_status, retry_count, error[:500], now, event_id,
        )

    async def get_event(self, event_id: str) -> OutboxEvent | None:
        assert self._conn is not None
        row = await self._conn.fetchrow("SELECT * FROM outbox_events WHERE event_id = $1", event_id)
        return self._row_to_event(row) if row else None

    async def get_pending_count(self) -> int:
        """Get the number of undelivered events."""
        assert self._conn is not None
        row = await self._conn.fetchrow(
            "SELECT COUNT(*) as cnt FROM outbox_events WHERE status = 'PENDING'"
        )
        return row["cnt"] if row else 0

    @staticmethod
    def _row_to_event(row: Any) -> OutboxEvent:
        return OutboxEvent(
            event_id=row["event_id"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            event_type=row["event_type"],
            payload=json.loads(row["payload"]) if row["payload"] else {},
            status=OutboxStatus(row["status"]),
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            last_error=row["last_error"],
            created_at=row["created_at"],
        )
