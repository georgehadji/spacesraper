# SQLite adapter for RecordRepository port.
# Stores ExtractedRecord with cursor-based pagination support.

import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Tuple

import aiosqlite

from src.domain.models import ExtractedRecord, ChangeType

logger = logging.getLogger("Spacescraper.RecordRepository")

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
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    change_type TEXT NOT NULL DEFAULT 'NEW',
    extracted_at TEXT NOT NULL,
    job_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

CREATE_RECORDS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_records_job ON records(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_records_job_created ON records(job_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_records_identity_hash ON records(identity_hash)",
]


class SqliteRecordRepository:
    """SQLite-backed implementation of RecordRepository."""

    def __init__(self, db_path: str = "spacescraper_jobs.db"):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def initialize(self):
        """Create tables and indexes if they don't exist."""
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute(CREATE_RECORDS_TABLE)
        for idx in CREATE_RECORDS_INDEXES:
            await self._conn.execute(idx)
        await self._conn.commit()
        logger.info("Record repository initialized at %s", self.db_path)

    async def close(self):
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def create_record(self, record: ExtractedRecord, job_id: str = "") -> ExtractedRecord:
        """Persist a new extracted record."""
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO records
               (record_id, record_type, schema_version, canonical_url, source_url, data,
                identity_hash, content_hash, first_seen, last_seen, change_type, extracted_at, job_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.record_id, record.record_type, record.schema_version,
                record.canonical_url, record.source_url,
                json.dumps(record.data, default=str),
                record.identity_hash, record.content_hash,
                record.first_seen.isoformat(), record.last_seen.isoformat(),
                record.change_type.value if isinstance(record.change_type, ChangeType) else record.change_type,
                record.extracted_at.isoformat(), job_id,
            ),
        )
        await self._conn.commit()
        return record

    async def get_record(self, record_id: str) -> Optional[ExtractedRecord]:
        """Retrieve a record by its ID."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM records WHERE record_id = ?", (record_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return self._row_to_record(row) if row else None

    async def list_records(
        self, job_id: str, *, cursor: Optional[str] = None, limit: int = 50
    ) -> Tuple[List[ExtractedRecord], Optional[str]]:
        """
        List records for a job with cursor-based pagination.
        Cursor is the record_id of the last item from the previous page.
        """
        assert self._conn is not None
        if cursor:
            async with self._conn.execute(
                """SELECT * FROM records
                   WHERE job_id = ? AND record_id > ?
                   ORDER BY record_id ASC LIMIT ?""",
                (job_id, cursor, limit + 1),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with self._conn.execute(
                """SELECT * FROM records
                   WHERE job_id = ?
                   ORDER BY record_id ASC LIMIT ?""",
                (job_id, limit + 1),
            ) as cur:
                rows = await cur.fetchall()

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        records = [self._row_to_record(r) for r in rows]
        next_cursor = rows[-1]["record_id"] if has_more and rows else None
        return records, next_cursor

    async def update_record(
        self, record_id: str, *,
        data: Optional[dict] = None,
        change_type: Optional[str] = None,
        last_seen: Optional[str] = None,
    ) -> Optional[ExtractedRecord]:
        """Update a record's mutable fields."""
        assert self._conn is not None
        now = datetime.now(timezone.utc)
        sets = ["last_seen = ?"]
        params = [now.isoformat()]

        if data is not None:
            sets.append("data = ?")
            params.append(json.dumps(data, default=str))
        if change_type is not None:
            sets.append("change_type = ?")
            params.append(change_type)
        if last_seen is not None:
            sets.append("last_seen = ?")
            params.append(last_seen)

        params.append(record_id)
        await self._conn.execute(
            f"UPDATE records SET {', '.join(sets)} WHERE record_id = ?",
            params,
        )
        await self._conn.commit()
        return await self.get_record(record_id)

    async def get_record_count(self, job_id: str) -> int:
        """Get the number of records for a job."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COUNT(*) as cnt FROM records WHERE job_id = ?", (job_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row["cnt"] if row else 0

    # --- helpers ---

    @staticmethod
    def _row_to_record(row) -> ExtractedRecord:
        return ExtractedRecord(
            record_id=row["record_id"],
            record_type=row["record_type"],
            schema_version=row["schema_version"],
            canonical_url=row["canonical_url"],
            source_url=row["source_url"],
            data=json.loads(row["data"]) if row["data"] else {},
            identity_hash=row["identity_hash"],
            content_hash=row["content_hash"],
            first_seen=datetime.fromisoformat(row["first_seen"]),
            last_seen=datetime.fromisoformat(row["last_seen"]),
            change_type=ChangeType(row["change_type"]) if row["change_type"] else ChangeType.NEW,
            extracted_at=datetime.fromisoformat(row["extracted_at"]),
        )
