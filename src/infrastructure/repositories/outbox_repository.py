# SQLite adapter for OutboxRepository port.
# Stores pending events for reliable relay to Valkey Streams.

import json
import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

from src.domain.models import OutboxEvent, OutboxStatus

logger = logging.getLogger("Spacescraper.OutboxRepository")

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
    last_attempt_at TEXT,
    created_at TEXT NOT NULL
)
"""

CREATE_OUTBOX_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox_events(status)",
    "CREATE INDEX IF NOT EXISTS idx_outbox_created ON outbox_events(created_at)",
]


class SqliteOutboxRepository:
    """SQLite-backed implementation of OutboxRepository."""

    def __init__(self, db_path: str = "spacescraper_jobs.db"):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self):
        """Create tables and indexes if they don't exist."""
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute(CREATE_OUTBOX_TABLE)
        for idx in CREATE_OUTBOX_INDEXES:
            await self._conn.execute(idx)
        await self._conn.commit()
        logger.info("Outbox repository initialized at %s", self.db_path)

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def create_event(
        self, event: OutboxEvent, *, conn: aiosqlite.Connection | None = None, commit: bool = True
    ) -> OutboxEvent:
        """Persist a new outbox event.

        conn lets a caller (e.g. main.py's job-submission unit of work) write
        this insert on another repository's connection to the same SQLite
        file, so it lands in that connection's transaction instead of its own.
        """
        connection = conn if conn is not None else self._conn
        assert connection is not None
        await connection.execute(
            """INSERT INTO outbox_events
               (event_id, aggregate_type, aggregate_id, event_type, payload,
                status, retry_count, max_retries, last_error, last_attempt_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id, event.aggregate_type, event.aggregate_id, event.event_type,
                json.dumps(event.payload, default=str),
                event.status.value, event.retry_count, event.max_retries,
                event.last_error, None, event.created_at.isoformat(),
            ),
        )
        if commit:
            await connection.commit()
        return event

    async def get_pending_events(
        self, limit: int = 50, min_retry_delay_seconds: int = 10
    ) -> list[OutboxEvent]:
        assert self._conn is not None
        cutoff = (datetime.now(UTC) - timedelta(seconds=min_retry_delay_seconds)).isoformat()
        async with self._conn.execute(
            """SELECT * FROM outbox_events
               WHERE status = 'PENDING'
               AND (last_attempt_at IS NULL OR last_attempt_at < ?)
               ORDER BY created_at ASC LIMIT ?""",
            (cutoff, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_event(r) for r in rows]

    async def mark_delivered(self, event_id: str) -> None:
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        await self._conn.execute(
            "UPDATE outbox_events SET status = 'DELIVERED', last_attempt_at = ? WHERE event_id = ?",
            (now, event_id),
        )
        await self._conn.commit()

    async def mark_failed(self, event_id: str, error: str) -> None:
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        # Atomically increment retry_count and check if maxed
        async with self._conn.execute(
            "SELECT retry_count, max_retries FROM outbox_events WHERE event_id = ?",
            (event_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return
            retry_count = row["retry_count"] + 1
            max_retries = row["max_retries"]
            new_status = "FAILED" if retry_count >= max_retries else "PENDING"

        await self._conn.execute(
            """UPDATE outbox_events
               SET status = ?, retry_count = ?, last_error = ?, last_attempt_at = ?
               WHERE event_id = ?""",
            (new_status, retry_count, error[:500], now, event_id),
        )
        await self._conn.commit()

    async def get_event(self, event_id: str) -> OutboxEvent | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM outbox_events WHERE event_id = ?", (event_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return self._row_to_event(row) if row else None

    async def get_pending_count(self) -> int:
        """Get the number of undelivered events."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COUNT(*) as cnt FROM outbox_events WHERE status = 'PENDING'"
        ) as cursor:
            row = await cursor.fetchone()
            return row["cnt"] if row else 0

    @staticmethod
    def _row_to_event(row) -> OutboxEvent:
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
            created_at=datetime.fromisoformat(row["created_at"]),
        )
