# Postgres adapter for RecordRepository port (C8/W5.3).
# Mirrors SqliteRecordRepository — see postgres_job_repository.py's module
# docstring for the single-connection design rationale.

import json
import logging
from datetime import UTC, datetime
from typing import Any

import asyncpg

from src.config_settings import settings
from src.domain.models import ChangeType, ExtractedRecord
from src.infrastructure.repositories.postgres_conn import PostgresConnection, asyncpg_dsn, create_pool_with_retry

logger = logging.getLogger("Spacescraper.PostgresRecordRepository")

CREATE_RECORDS_TABLE = """
CREATE TABLE IF NOT EXISTS records (
    record_id TEXT PRIMARY KEY,
    record_type TEXT NOT NULL DEFAULT 'generic',
    schema_version TEXT NOT NULL DEFAULT '1.0',
    canonical_url TEXT,
    source_url TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    identity_hash TEXT,
    content_hash TEXT,
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL,
    change_type TEXT NOT NULL DEFAULT 'NEW',
    extracted_at TIMESTAMPTZ NOT NULL,
    job_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

CREATE_RECORDS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_records_job ON records(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_records_job_created ON records(job_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_records_identity_hash ON records(identity_hash)",
]


class PostgresRecordRepository:
    """Postgres-backed implementation of RecordRepository. Mirrors SqliteRecordRepository."""

    def __init__(self, dsn: str):
        self.dsn = asyncpg_dsn(dsn)
        self._pool: asyncpg.Pool | None = None
        self._conn: PostgresConnection | None = None

    async def initialize(self) -> None:
        self._pool = await create_pool_with_retry(
            self.dsn, min_size=2, max_size=settings.database.pool_size + settings.database.max_overflow,
        )
        self._conn = PostgresConnection(self._pool)
        await self._conn.execute(CREATE_RECORDS_TABLE)
        for idx in CREATE_RECORDS_INDEXES:
            await self._conn.execute(idx)
        logger.info("Record repository initialized at Postgres")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            self._conn = None

    async def create_record(self, record: ExtractedRecord, job_id: str) -> ExtractedRecord:
        """Persist a new extracted record."""
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO records
               (record_id, record_type, schema_version, canonical_url, source_url, data,
                identity_hash, content_hash, first_seen, last_seen, change_type, extracted_at, job_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)""",
            record.record_id, record.record_type, record.schema_version,
            record.canonical_url, record.source_url,
            json.dumps(record.data, default=str),
            record.identity_hash, record.content_hash,
            record.first_seen, record.last_seen,
            record.change_type.value if isinstance(record.change_type, ChangeType) else record.change_type,
            record.extracted_at, job_id,
        )
        return record

    async def get_record(self, record_id: str) -> ExtractedRecord | None:
        """Retrieve a record by its ID."""
        assert self._conn is not None
        row = await self._conn.fetchrow("SELECT * FROM records WHERE record_id = $1", record_id)
        return self._row_to_record(row) if row else None

    async def list_records(
        self, job_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[ExtractedRecord], str | None]:
        """
        List records for a job with cursor-based pagination.
        Cursor is the record_id of the last item from the previous page.
        """
        assert self._conn is not None
        if cursor:
            rows = await self._conn.fetch(
                """SELECT * FROM records
                   WHERE job_id = $1 AND record_id > $2
                   ORDER BY record_id ASC LIMIT $3""",
                job_id, cursor, limit + 1,
            )
        else:
            rows = await self._conn.fetch(
                """SELECT * FROM records
                   WHERE job_id = $1
                   ORDER BY record_id ASC LIMIT $2""",
                job_id, limit + 1,
            )

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        records = [self._row_to_record(r) for r in rows]
        next_cursor = rows[-1]["record_id"] if has_more and rows else None
        return records, next_cursor

    async def update_record(
        self, record_id: str, *,
        data: dict[str, Any] | None = None,
        change_type: str | None = None,
        last_seen: str | None = None,
    ) -> ExtractedRecord | None:
        """Update a record's mutable fields."""
        assert self._conn is not None
        now = datetime.now(UTC)
        sets = ["last_seen = $1"]
        params: list[Any] = [now]

        if data is not None:
            params.append(json.dumps(data, default=str))
            sets.append(f"data = ${len(params)}")
        if change_type is not None:
            params.append(change_type)
            sets.append(f"change_type = ${len(params)}")
        if last_seen is not None:
            # The port declares last_seen: str (an ISO string) — parse it to
            # a datetime, overriding the $1 default set above. asyncpg (unlike
            # SQLite's TEXT column) needs the native type for TIMESTAMPTZ.
            params.append(datetime.fromisoformat(last_seen))
            sets.append(f"last_seen = ${len(params)}")

        params.append(record_id)
        # `sets` entries are fixed literals from the branches above, never derived
        # from caller input; all values are bound via $n params.
        await self._conn.execute(
            f"UPDATE records SET {', '.join(sets)} WHERE record_id = ${len(params)}",  # nosec B608
            *params,
        )
        return await self.get_record(record_id)

    async def get_record_count(self, job_id: str) -> int:
        """Get the number of records for a job."""
        assert self._conn is not None
        row = await self._conn.fetchrow("SELECT COUNT(*) as cnt FROM records WHERE job_id = $1", job_id)
        return row["cnt"] if row else 0

    # --- helpers ---

    @staticmethod
    def _row_to_record(row: Any) -> ExtractedRecord:
        return ExtractedRecord(
            record_id=row["record_id"],
            record_type=row["record_type"],
            schema_version=row["schema_version"],
            canonical_url=row["canonical_url"],
            source_url=row["source_url"],
            data=json.loads(row["data"]) if row["data"] else {},
            identity_hash=row["identity_hash"],
            content_hash=row["content_hash"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            change_type=ChangeType(row["change_type"]) if row["change_type"] else ChangeType.NEW,
            extracted_at=row["extracted_at"],
        )
