# SQLite adapter for JobRepository port.
# Uses aiosqlite with WAL mode for concurrent reads.

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from src.domain.models import Job, JobAttempt, JobState

logger = logging.getLogger("Spacescraper.JobRepository")

# Chunk size for purge_expired_jobs' batched DELETE ... WHERE job_id IN (...).
# SQLite's default host-parameter limit is in the thousands, but this stays
# well under it for any driver/version, and keeps a first-run backlog (this
# method was unreachable until it existed) from building one giant statement.
PURGE_BATCH_SIZE = 500

# Migration: table schemas
CREATE_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    target_site TEXT NOT NULL DEFAULT 'universal',
    state TEXT NOT NULL DEFAULT 'QUEUED',
    priority INTEGER NOT NULL DEFAULT 0,
    max_depth INTEGER NOT NULL DEFAULT 3,
    overlay TEXT,
    webhook_url TEXT,
    correlation_id TEXT,
    record_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT UNIQUE,
    retention_days INTEGER,
    deleted_at TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

CREATE_JOB_ATTEMPTS_TABLE = """
CREATE TABLE IF NOT EXISTS job_attempts (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    state TEXT NOT NULL DEFAULT 'RUNNING',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    worker_id TEXT,
    error_message TEXT
)
"""

CREATE_JOBS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_job_attempts_job ON job_attempts(job_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency_key ON jobs(idempotency_key)",
]


class SqliteJobRepository:
    """SQLite-backed implementation of JobRepository."""

    def __init__(self, db_path: str = "spacescraper_jobs.db"):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Create tables and indexes if they don't exist."""
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute(CREATE_JOBS_TABLE)
        await self._conn.execute(CREATE_JOB_ATTEMPTS_TABLE)
        # Schema migration: add version column if missing (pre-v3 databases)
        try:
            await self._conn.execute("ALTER TABLE jobs ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass  # column already exists
        # Schema migration: add idempotency_key if missing.
        # SQLite's ALTER TABLE ADD COLUMN rejects inline UNIQUE constraints,
        # so the column is added plain and uniqueness is enforced via index.
        try:
            await self._conn.execute("ALTER TABLE jobs ADD COLUMN idempotency_key TEXT")
        except Exception:
            pass
        # Schema migration: add retention_days and deleted_at if missing
        try:
            await self._conn.execute("ALTER TABLE jobs ADD COLUMN retention_days INTEGER")
        except Exception:
            pass
        try:
            await self._conn.execute("ALTER TABLE jobs ADD COLUMN deleted_at TEXT")
        except Exception:
            pass
        # Schema migration: add last_heartbeat_at if missing
        try:
            await self._conn.execute("ALTER TABLE jobs ADD COLUMN last_heartbeat_at TEXT")
        except Exception:
            pass
        for idx in CREATE_JOBS_INDEXES:
            await self._conn.execute(idx)
        await self._conn.commit()
        logger.info("Job repository initialized at %s", self.db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """R-W1/F14: yields self._conn unchanged — a single aiosqlite
        connection already holds an implicit transaction open across
        statements until commit()/rollback(), so there is nothing to
        acquire. Commits on a clean exit, rolls back if the block raises.
        Pass the yielded value to create_job's/OutboxRepository.create_event's
        conn= so both writes land in this same transaction."""
        assert self._conn is not None
        try:
            yield self._conn
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def create_job(self, job: Job, *, conn: aiosqlite.Connection | None = None) -> Job:
        """Persist a new job record.

        conn, when given (the value yielded by transaction()), makes this
        insert join that transaction instead of auto-committing on its own —
        lets a caller (e.g. main.py's job-submission unit of work) keep this
        insert and a following one atomic together.
        """
        assert self._conn is not None
        connection = conn if conn is not None else self._conn
        await connection.execute(
            """INSERT INTO jobs (job_id, url, target_site, state, priority, max_depth,
                                 overlay, webhook_url, correlation_id, record_count,
                                 error_message, idempotency_key, version, retention_days,
                                 deleted_at, last_heartbeat_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job.job_id, job.url, job.target_site, job.state.value,
                job.priority, job.max_depth,
                json.dumps(job.overlay) if job.overlay else None,
                job.webhook_url, job.correlation_id, job.record_count,
                job.error_message,
                job.idempotency_key,
                job.version, job.retention_days,
                job.deleted_at.isoformat() if job.deleted_at else None,
                job.last_heartbeat_at.isoformat() if job.last_heartbeat_at else None,
                job.created_at.isoformat(), job.updated_at.isoformat(),
            ),
        )
        if conn is None:
            await self._conn.commit()
        return job

    async def get_job(self, job_id: str) -> Job | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_job(row)

    async def get_by_idempotency_key(self, key: str) -> Job | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_job(row)

    async def heartbeat(self, job_id: str) -> None:
        """Update last_heartbeat_at for a job to signal worker is alive."""
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        await self._conn.execute(
            "UPDATE jobs SET last_heartbeat_at = ? WHERE job_id = ?",
            (now, job_id),
        )
        await self._conn.commit()

    async def find_stale_jobs(self, stale_seconds: int = 120, limit: int = 50) -> list[Job]:
        """Find RUNNING jobs whose last_heartbeat_at is older than stale_seconds."""
        assert self._conn is not None
        cutoff = (datetime.now(tz=UTC) - timedelta(seconds=stale_seconds)).isoformat()
        async with self._conn.execute(
            "SELECT * FROM jobs WHERE state = 'RUNNING' AND (last_heartbeat_at IS NULL OR last_heartbeat_at < ?) LIMIT ?",
            (cutoff, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_job(r) for r in rows]

    async def update_job_state(
        self, job_id: str, new_state: JobState,
        *, expected_version: int, error_message: str | None = None
    ) -> Job | None:
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        cursor = await self._conn.execute(
            "UPDATE jobs SET state = ?, version = version + 1, updated_at = ?, error_message = ? WHERE job_id = ? AND version = ?",
            (new_state.value, now, error_message, job_id, expected_version),
        )
        await self._conn.commit()
        if cursor.rowcount == 0:
            return None  # version conflict or job not found
        return await self.get_job(job_id)

    async def update_job_record_count(self, job_id: str, count: int) -> None:
        assert self._conn is not None
        now = datetime.now(UTC).isoformat()
        await self._conn.execute(
            "UPDATE jobs SET record_count = ?, updated_at = ? WHERE job_id = ?",
            (count, now, job_id),
        )
        await self._conn.commit()

    async def list_jobs(
        self, state: JobState | None = None,
        limit: int = 50, offset: int = 0
    ) -> list[Job]:
        assert self._conn is not None
        if state:
            async with self._conn.execute(
                "SELECT * FROM jobs WHERE state = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (state.value, limit, offset),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ) as cursor:
                rows = await cursor.fetchall()
        return [self._row_to_job(r) for r in rows]

    async def create_attempt(self, attempt: JobAttempt) -> JobAttempt:
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO job_attempts
               (attempt_id, job_id, state, started_at, finished_at, worker_id, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                attempt.attempt_id, attempt.job_id, attempt.state.value,
                attempt.started_at.isoformat(), attempt.finished_at.isoformat() if attempt.finished_at else None,
                attempt.worker_id, attempt.error_message,
            ),
        )
        await self._conn.commit()
        return attempt

    async def update_attempt(
        self, attempt_id: str, *, state: JobState | None = None,
        finished_at: str | None = None, error_message: str | None = None
    ) -> JobAttempt | None:
        assert self._conn is not None
        # Single atomic UPDATE with only the provided fields
        sets = []
        params = []
        if state is not None:
            sets.append("state = ?")
            params.append(state.value)
        if finished_at is not None:
            sets.append("finished_at = ?")
            params.append(finished_at)
        if error_message is not None:
            sets.append("error_message = ?")
            params.append(error_message)
        if sets:
            params.append(attempt_id)
            # `sets` entries are fixed literals from the branches above, never derived
            # from caller input; all values are bound via `?` params.
            await self._conn.execute(
                f"UPDATE job_attempts SET {', '.join(sets)} WHERE attempt_id = ?",  # nosec B608
                params,
            )
            await self._conn.commit()
        async with self._conn.execute(
            "SELECT * FROM job_attempts WHERE attempt_id = ?", (attempt_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return self._row_to_attempt(row) if row else None

    async def get_attempts(self, job_id: str) -> list[JobAttempt]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM job_attempts WHERE job_id = ? ORDER BY started_at DESC",
            (job_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_attempt(r) for r in rows]

    async def soft_delete_job(self, job_id: str) -> Job | None:
        """Soft-delete a job by transitioning it to DELETED and stamping deleted_at.

        Only valid from a terminal state (SUCCEEDED/FAILED/CANCELLED/DEAD_LETTERED),
        per Job.can_transition_to — mirrors update_job_state's optimistic-concurrency
        pattern via the version check.
        """
        assert self._conn is not None
        job = await self.get_job(job_id)
        if job is None or not job.state.can_transition_to(JobState.DELETED):
            return None
        now = datetime.now(UTC).isoformat()
        cursor = await self._conn.execute(
            """UPDATE jobs SET state = ?, deleted_at = ?, version = version + 1, updated_at = ?
               WHERE job_id = ? AND version = ?""",
            (JobState.DELETED.value, now, now, job_id, job.version),
        )
        await self._conn.commit()
        if cursor.rowcount == 0:
            return None  # version conflict — job changed between the read above and here
        return await self.get_job(job_id)

    async def purge_expired_jobs(self, retention_days: int = 90) -> int:
        """Hard-delete jobs soft-deleted longer than retention_days ago.

        A job's own `retention_days` (Job.retention_days) overrides the parameter
        when set. Deletes job_attempts rows first — Postgres enforces the
        job_attempts -> jobs foreign key even though SQLite doesn't, so both
        adapters must clear children before the parent to behave the same way.
        Returns the number of jobs purged.
        """
        assert self._conn is not None
        now = datetime.now(UTC)
        async with self._conn.execute(
            "SELECT job_id, deleted_at, retention_days FROM jobs WHERE deleted_at IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
        expired_ids = [
            row["job_id"]
            for row in rows
            if (now - datetime.fromisoformat(row["deleted_at"]))
            >= timedelta(days=row["retention_days"] if row["retention_days"] is not None else retention_days)
        ]
        if not expired_ids:
            return 0
        purged = 0
        for batch in (expired_ids[i : i + PURGE_BATCH_SIZE] for i in range(0, len(expired_ids), PURGE_BATCH_SIZE)):
            # `placeholders` is a run of `?` marks sized from a Python-computed
            # batch length, not from caller-supplied text; every value is still
            # bound as a parameter below.
            placeholders = ", ".join("?" for _ in batch)
            await self._conn.execute(
                f"DELETE FROM job_attempts WHERE job_id IN ({placeholders})", batch  # nosec B608
            )
            await self._conn.execute(
                f"DELETE FROM jobs WHERE job_id IN ({placeholders})", batch  # nosec B608
            )
            purged += len(batch)
        await self._conn.commit()
        return purged

    # --- helpers ---

    @staticmethod
    def _row_to_job(row: Any) -> Job:
        return Job(
            job_id=row["job_id"],
            url=row["url"],
            target_site=row["target_site"],
            state=JobState(row["state"]),
            priority=row["priority"],
            max_depth=row["max_depth"],
            overlay=json.loads(row["overlay"]) if row["overlay"] else None,
            webhook_url=row["webhook_url"],
            correlation_id=row["correlation_id"],
            record_count=row["record_count"],
            error_message=row["error_message"],
            idempotency_key=row["idempotency_key"] if "idempotency_key" in row.keys() else None,
            version=row["version"],
            retention_days=row["retention_days"] if "retention_days" in row.keys() else None,
            deleted_at=datetime.fromisoformat(row["deleted_at"]) if "deleted_at" in row.keys() and row["deleted_at"] else None,
            last_heartbeat_at=datetime.fromisoformat(row["last_heartbeat_at"]) if "last_heartbeat_at" in row.keys() and row["last_heartbeat_at"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _row_to_attempt(row: Any) -> JobAttempt:
        return JobAttempt(
            attempt_id=row["attempt_id"],
            job_id=row["job_id"],
            state=JobState(row["state"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            worker_id=row["worker_id"],
            error_message=row["error_message"],
        )
