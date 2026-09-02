# Postgres adapter for JobRepository port (C8/W5.3, R-W1).
# Pool-backed (see postgres_conn.py's module docstring) — every method still
# reads as `await self._conn.execute(...)` etc., but self._conn now acquires
# a connection from the pool per call instead of holding one bare connection
# for the process lifetime. transaction() is the one place that acquires and
# holds a single connection deliberately, for main.py's F14 unit of work.

import json
import logging
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from src.config_settings import settings
from src.domain.models import Job, JobAttempt, JobState
from src.infrastructure.repositories.postgres_conn import (
    PostgresConnection,
    PostgresTransaction,
    asyncpg_dsn,
    create_pool_with_retry,
    transaction_scope,
)

logger = logging.getLogger("Spacescraper.PostgresJobRepository")

# See job_repository.py's PURGE_BATCH_SIZE — same reasoning, kept in sync.
PURGE_BATCH_SIZE = 500

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
    deleted_at TIMESTAMPTZ,
    last_heartbeat_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
)
"""

CREATE_JOB_ATTEMPTS_TABLE = """
CREATE TABLE IF NOT EXISTS job_attempts (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    state TEXT NOT NULL DEFAULT 'RUNNING',
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    worker_id TEXT,
    error_message TEXT
)
"""

CREATE_JOBS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_job_attempts_job ON job_attempts(job_id)",
]


class PostgresJobRepository:
    """Postgres-backed implementation of JobRepository. Mirrors SqliteJobRepository."""

    def __init__(self, dsn: str):
        self.dsn = asyncpg_dsn(dsn)
        self._pool: asyncpg.Pool | None = None
        self._conn: PostgresConnection | None = None

    async def initialize(self) -> None:
        self._pool = await create_pool_with_retry(
            self.dsn, min_size=2, max_size=settings.database.pool_size + settings.database.max_overflow,
        )
        self._conn = PostgresConnection(self._pool)
        await self._conn.execute(CREATE_JOBS_TABLE)
        await self._conn.execute(CREATE_JOB_ATTEMPTS_TABLE)
        for idx in CREATE_JOBS_INDEXES:
            await self._conn.execute(idx)
        logger.info("Job repository initialized at Postgres")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            self._conn = None

    def transaction(self) -> AbstractAsyncContextManager[PostgresTransaction]:
        """R-W1/F14: acquires one pool connection and holds it, with a real
        transaction open, for the life of the `async with` block. Pass the
        yielded value to create_job's/OutboxRepository.create_event's conn=
        so both writes land in this same transaction."""
        assert self._pool is not None
        return transaction_scope(self._pool)

    async def create_job(self, job: Job, *, conn: Any = None) -> Job:
        """Persist a new job record.

        conn, when given (the value yielded by transaction()), makes this
        insert run on that held connection instead of self._conn's per-call
        pool acquisition — joining that transaction instead of auto-
        committing on its own.
        """
        assert self._conn is not None
        target = conn if conn is not None else self._conn
        await target.execute(
            """INSERT INTO jobs (job_id, url, target_site, state, priority, max_depth,
                                 overlay, webhook_url, correlation_id, record_count,
                                 error_message, idempotency_key, version, retention_days,
                                 deleted_at, last_heartbeat_at, created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)""",
            job.job_id, job.url, job.target_site, job.state.value,
            job.priority, job.max_depth,
            json.dumps(job.overlay) if job.overlay else None,
            job.webhook_url, job.correlation_id, job.record_count,
            job.error_message,
            job.idempotency_key,
            job.version, job.retention_days,
            job.deleted_at, job.last_heartbeat_at,
            job.created_at, job.updated_at,
        )
        return job

    async def get_job(self, job_id: str) -> Job | None:
        assert self._conn is not None
        row = await self._conn.fetchrow("SELECT * FROM jobs WHERE job_id = $1", job_id)
        return self._row_to_job(row) if row else None

    async def get_by_idempotency_key(self, key: str) -> Job | None:
        assert self._conn is not None
        row = await self._conn.fetchrow("SELECT * FROM jobs WHERE idempotency_key = $1", key)
        return self._row_to_job(row) if row else None

    async def heartbeat(self, job_id: str) -> None:
        assert self._conn is not None
        now = datetime.now(UTC)
        await self._conn.execute(
            "UPDATE jobs SET last_heartbeat_at = $1 WHERE job_id = $2", now, job_id,
        )

    async def find_stale_jobs(self, stale_seconds: int = 120, limit: int = 50) -> list[Job]:
        assert self._conn is not None
        cutoff = datetime.now(tz=UTC) - timedelta(seconds=stale_seconds)
        rows = await self._conn.fetch(
            "SELECT * FROM jobs WHERE state = 'RUNNING' AND (last_heartbeat_at IS NULL OR last_heartbeat_at < $1) LIMIT $2",
            cutoff, limit,
        )
        return [self._row_to_job(r) for r in rows]

    async def update_job_state(
        self, job_id: str, new_state: JobState,
        *, expected_version: int, error_message: str | None = None
    ) -> Job | None:
        assert self._conn is not None
        now = datetime.now(UTC)
        row = await self._conn.fetchrow(
            """UPDATE jobs SET state = $1, version = version + 1, updated_at = $2, error_message = $3
               WHERE job_id = $4 AND version = $5 RETURNING *""",
            new_state.value, now, error_message, job_id, expected_version,
        )
        return self._row_to_job(row) if row else None  # None = version conflict or not found

    async def update_job_record_count(self, job_id: str, count: int) -> None:
        assert self._conn is not None
        now = datetime.now(UTC)
        await self._conn.execute(
            "UPDATE jobs SET record_count = $1, updated_at = $2 WHERE job_id = $3",
            count, now, job_id,
        )

    async def list_jobs(
        self, state: JobState | None = None,
        limit: int = 50, offset: int = 0
    ) -> list[Job]:
        assert self._conn is not None
        if state:
            rows = await self._conn.fetch(
                "SELECT * FROM jobs WHERE state = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                state.value, limit, offset,
            )
        else:
            rows = await self._conn.fetch(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT $1 OFFSET $2", limit, offset,
            )
        return [self._row_to_job(r) for r in rows]

    async def create_attempt(self, attempt: JobAttempt) -> JobAttempt:
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO job_attempts
               (attempt_id, job_id, state, started_at, finished_at, worker_id, error_message)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            attempt.attempt_id, attempt.job_id, attempt.state.value,
            attempt.started_at, attempt.finished_at,
            attempt.worker_id, attempt.error_message,
        )
        return attempt

    async def update_attempt(
        self, attempt_id: str, *, state: JobState | None = None,
        finished_at: str | None = None, error_message: str | None = None
    ) -> JobAttempt | None:
        assert self._conn is not None
        sets = []
        params: list[Any] = []
        if state is not None:
            params.append(state.value)
            sets.append(f"state = ${len(params)}")
        if finished_at is not None:
            # The port declares finished_at: str (an ISO string) — parse it
            # to a datetime here since finished_at is now TIMESTAMPTZ, and
            # asyncpg (unlike SQLite's TEXT column) needs the native type.
            params.append(datetime.fromisoformat(finished_at))
            sets.append(f"finished_at = ${len(params)}")
        if error_message is not None:
            params.append(error_message)
            sets.append(f"error_message = ${len(params)}")
        if sets:
            params.append(attempt_id)
            # `sets` entries are fixed literals from the branches above, never derived
            # from caller input; all values are bound via $n params.
            await self._conn.execute(
                f"UPDATE job_attempts SET {', '.join(sets)} WHERE attempt_id = ${len(params)}",  # nosec B608
                *params,
            )
        row = await self._conn.fetchrow("SELECT * FROM job_attempts WHERE attempt_id = $1", attempt_id)
        return self._row_to_attempt(row) if row else None

    async def get_attempts(self, job_id: str) -> list[JobAttempt]:
        assert self._conn is not None
        rows = await self._conn.fetch(
            "SELECT * FROM job_attempts WHERE job_id = $1 ORDER BY started_at DESC", job_id,
        )
        return [self._row_to_attempt(r) for r in rows]

    async def soft_delete_job(self, job_id: str) -> Job | None:
        """Soft-delete a job by transitioning it to DELETED and stamping deleted_at.

        Mirrors SqliteJobRepository.soft_delete_job — see there for the state-machine
        and optimistic-concurrency rationale.
        """
        assert self._conn is not None
        job = await self.get_job(job_id)
        if job is None or not job.state.can_transition_to(JobState.DELETED):
            return None
        now = datetime.now(UTC)
        row = await self._conn.fetchrow(
            """UPDATE jobs SET state = $1, deleted_at = $2, version = version + 1, updated_at = $3
               WHERE job_id = $4 AND version = $5 RETURNING *""",
            JobState.DELETED.value, now, now, job_id, job.version,
        )
        return self._row_to_job(row) if row else None  # None = version conflict

    async def purge_expired_jobs(self, retention_days: int = 90) -> int:
        """Hard-delete jobs soft-deleted longer than retention_days ago.

        Mirrors SqliteJobRepository.purge_expired_jobs. Deleting job_attempts
        first is not optional here: Postgres enforces the job_attempts -> jobs
        foreign key, so deleting a job with attempts still present raises
        ForeignKeyViolationError.
        """
        assert self._conn is not None
        now = datetime.now(UTC)
        rows = await self._conn.fetch(
            "SELECT job_id, deleted_at, retention_days FROM jobs WHERE deleted_at IS NOT NULL"
        )
        expired_ids = [
            row["job_id"]
            for row in rows
            if (now - row["deleted_at"])
            >= timedelta(days=row["retention_days"] if row["retention_days"] is not None else retention_days)
        ]
        if not expired_ids:
            return 0
        purged = 0
        for batch in (expired_ids[i : i + PURGE_BATCH_SIZE] for i in range(0, len(expired_ids), PURGE_BATCH_SIZE)):
            # `placeholders` is a run of `$n` marks sized from a Python-computed
            # batch length, not from caller-supplied text; every value is still
            # bound as a parameter below.
            placeholders = ", ".join(f"${i + 1}" for i in range(len(batch)))
            await self._conn.execute(
                f"DELETE FROM job_attempts WHERE job_id IN ({placeholders})", *batch  # nosec B608
            )
            await self._conn.execute(
                f"DELETE FROM jobs WHERE job_id IN ({placeholders})", *batch  # nosec B608
            )
            purged += len(batch)
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
            idempotency_key=row["idempotency_key"],
            version=row["version"],
            retention_days=row["retention_days"],
            deleted_at=row["deleted_at"],
            last_heartbeat_at=row["last_heartbeat_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_attempt(row: Any) -> JobAttempt:
        return JobAttempt(
            attempt_id=row["attempt_id"],
            job_id=row["job_id"],
            state=JobState(row["state"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            worker_id=row["worker_id"],
            error_message=row["error_message"],
        )
