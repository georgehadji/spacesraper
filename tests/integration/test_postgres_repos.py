# Integration tests for the Postgres repository adapters (C8/W5.3).
#
# Skipped unless TEST_POSTGRES_URL is set — these are the one part of this
# change that couldn't be verified locally (Docker Desktop's daemon wasn't
# running), so treat a green run here as the real confirmation the
# hand-translated SQL (? -> $n, cursor.rowcount -> RETURNING+fetchrow) is
# actually correct against a real server, not just syntactically plausible.
# See docs/adr/0001-postgres-backend.md.
#
# Run locally against a throwaway Postgres:
#   docker run --rm -p 5433:5432 -e POSTGRES_PASSWORD=postgres postgres:16-alpine
#   TEST_POSTGRES_URL=postgresql://postgres:postgres@localhost:5433/postgres \
#       pytest tests/integration/test_postgres_repos.py -q

import asyncio
import os
import uuid

import pytest

from src.application.reaper import JobReaper
from src.domain.models import (
    ExtractedRecord,
    ExtractionOverlay,
    Job,
    JobAttempt,
    JobState,
    OutboxEvent,
    OutboxStatus,
    OverlayState,
    StrategyObservation,
)

TEST_DSN = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_POSTGRES_URL not set — no live Postgres to test against"
)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def job_repo():
    from src.infrastructure.repositories.postgres_job_repository import PostgresJobRepository

    repo = PostgresJobRepository(TEST_DSN)
    await repo.initialize()
    yield repo
    await repo.close()


@pytest.fixture
async def record_repo():
    from src.infrastructure.repositories.postgres_record_repository import PostgresRecordRepository

    repo = PostgresRecordRepository(TEST_DSN)
    await repo.initialize()
    yield repo
    await repo.close()


@pytest.fixture
async def outbox_repo():
    from src.infrastructure.repositories.postgres_outbox_repository import PostgresOutboxRepository

    repo = PostgresOutboxRepository(TEST_DSN)
    await repo.initialize()
    yield repo
    await repo.close()


@pytest.fixture
async def overlay_repo():
    from src.infrastructure.repositories.postgres_overlay_repository import PostgresOverlayRepository

    repo = PostgresOverlayRepository(TEST_DSN)
    await repo.initialize()
    yield repo
    await repo.close()


@pytest.fixture
async def obs_repo():
    from src.infrastructure.repositories.postgres_observation_repository import (
        PostgresObservationRepository,
    )

    repo = PostgresObservationRepository(TEST_DSN)
    await repo.initialize()
    yield repo
    await repo.close()


@pytest.mark.asyncio
async def test_job_create_get_and_optimistic_concurrency(job_repo):
    job = Job(job_id=_unique("job"), url="https://example.com/listing")
    await job_repo.create_job(job)

    fetched = await job_repo.get_job(job.job_id)
    assert fetched is not None
    assert fetched.url == job.url
    assert fetched.version == 1

    updated = await job_repo.update_job_state(job.job_id, JobState.RUNNING, expected_version=1)
    assert updated is not None
    assert updated.state == JobState.RUNNING
    assert updated.version == 2

    # Stale version must be rejected, not silently applied.
    conflict = await job_repo.update_job_state(job.job_id, JobState.SUCCEEDED, expected_version=1)
    assert conflict is None

    attempt = JobAttempt(attempt_id=_unique("att"), job_id=job.job_id, worker_id="w1")
    await job_repo.create_attempt(attempt)
    attempts = await job_repo.get_attempts(job.job_id)
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_timestamp_round_trips_as_timezone_aware(job_repo):
    """R-W5.4: created_at/updated_at/last_heartbeat_at are TIMESTAMPTZ, not
    TEXT (R9) — this is the regression this change is most likely to
    introduce: a naive datetime silently accepted, or a non-UTC offset
    normalized without the instant itself surviving. A non-UTC offset input
    is deliberate — UTC round-tripping correctly wouldn't catch a driver
    that silently drops tzinfo, since UTC's offset is zero either way."""
    from datetime import datetime, timedelta, timezone

    ist = timezone(timedelta(hours=5, minutes=30))
    original = datetime(2026, 6, 15, 9, 30, 0, tzinfo=ist)

    job = Job(job_id=_unique("job"), url="https://example.com/listing", created_at=original, updated_at=original)
    await job_repo.create_job(job)

    fetched = await job_repo.get_job(job.job_id)
    assert fetched is not None
    assert fetched.created_at.tzinfo is not None, "created_at came back naive — TIMESTAMPTZ round-trip broke"
    assert fetched.created_at == original, "same instant must survive regardless of offset normalization"

    await job_repo.heartbeat(job.job_id)
    heartbeat_fetched = await job_repo.get_job(job.job_id)
    assert heartbeat_fetched.last_heartbeat_at is not None
    assert heartbeat_fetched.last_heartbeat_at.tzinfo is not None


@pytest.mark.asyncio
async def test_record_list_with_cursor_pagination(record_repo):
    job_id = _unique("job")
    for i in range(3):
        await record_repo.create_record(
            ExtractedRecord(
                record_id=f"rec-{job_id}-{i}",
                source_url=f"https://example.com/{i}",
                data={"n": i},
            ),
            job_id=job_id,
        )

    page1, cursor = await record_repo.list_records(job_id, limit=2)
    assert len(page1) == 2
    assert cursor is not None

    page2, cursor2 = await record_repo.list_records(job_id, cursor=cursor, limit=2)
    assert len(page2) == 1
    assert cursor2 is None
    assert await record_repo.get_record_count(job_id) == 3


@pytest.mark.asyncio
async def test_outbox_pending_lifecycle(outbox_repo):
    event = OutboxEvent(
        event_id=_unique("evt"), aggregate_type="job", aggregate_id="job-1",
        event_type="job.submitted", payload={"url": "https://example.com"},
    )
    await outbox_repo.create_event(event)

    assert await outbox_repo.get_pending_count() >= 1
    pending = await outbox_repo.get_pending_events()
    assert any(e.event_id == event.event_id for e in pending)

    await outbox_repo.mark_delivered(event.event_id)
    stored = await outbox_repo.get_event(event.event_id)
    assert stored.status == OutboxStatus.DELIVERED


@pytest.mark.asyncio
async def test_overlay_promotion_and_active_lookup(overlay_repo):
    domain = _unique("example") + ".com"
    overlay = ExtractionOverlay(overlay_id=_unique("ov"), domain=domain, schema_id="s1")
    await overlay_repo.create_overlay(overlay)

    assert await overlay_repo.get_active_overlay(domain) is None

    await overlay_repo.update_overlay_state(overlay.overlay_id, OverlayState.SHADOW)
    await overlay_repo.update_overlay_state(overlay.overlay_id, OverlayState.ACTIVE)

    active = await overlay_repo.get_active_overlay(domain)
    assert active is not None
    assert active.overlay_id == overlay.overlay_id


@pytest.mark.asyncio
async def test_observation_and_profile_roundtrip(obs_repo):
    domain = _unique("example") + ".com"
    obs = StrategyObservation(
        observation_id=_unique("obs"), job_id="job-1", domain=domain, strategy="http",
    )
    await obs_repo.create_observation(obs)

    fetched = await obs_repo.get_observations(domain=domain)
    assert len(fetched) == 1

    profile = await obs_repo.get_or_create_profile(domain)
    assert profile.domain == domain
    profile.total_observations = 5
    await obs_repo.update_profile(profile)
    updated = await obs_repo.get_or_create_profile(domain)
    assert updated.total_observations == 5
    assert updated.profile_version == 2


@pytest.mark.asyncio
async def test_job_outbox_shared_connection_unit_of_work(job_repo, outbox_repo):
    """F14/R-W1 under Postgres: job insert + outbox insert share one
    transaction via job_repo.transaction(), and must both vanish together
    when the block raises between them — same guarantee main.py's
    submit_job relies on for the SQLite backend."""
    job = Job(job_id=_unique("job"), url="https://example.com/listing")
    event = OutboxEvent(
        event_id=_unique("evt"), aggregate_type="job", aggregate_id=job.job_id,
        event_type="job.submitted", payload={},
    )

    with pytest.raises(RuntimeError):
        async with job_repo.transaction() as tx:
            await job_repo.create_job(job, conn=tx)
            await outbox_repo.create_event(event, conn=tx)
            raise RuntimeError("simulated failure between the two writes")

    assert await job_repo.get_job(job.job_id) is None
    assert await outbox_repo.get_event(event.event_id) is None


@pytest.mark.asyncio
async def test_purge_once_actually_deletes_rows_end_to_end(job_repo):
    """R-W7.3, Postgres side — see tests/test_reaper.py's SQLite-side twin
    for the full rationale. Same soft-delete-with-retention_days=0 pattern,
    against the real PostgresJobRepository. Also implicitly re-covers R2
    (purge_expired_jobs missing on the Postgres adapter) and the
    job_attempts-before-jobs delete ordering Postgres's foreign key forces
    that SQLite doesn't."""
    job = Job(job_id=_unique("job"), url="https://example.com/listing", retention_days=0)
    await job_repo.create_job(job)
    await job_repo.update_job_state(job.job_id, JobState.SUCCEEDED, expected_version=1)
    deleted = await job_repo.soft_delete_job(job.job_id)
    assert deleted is not None and deleted.state == JobState.DELETED

    reaper = JobReaper(job_repo=job_repo)
    purged = await reaper.purge_once(retention_days=90)  # job's own retention_days=0 overrides this

    assert purged == 1
    assert await job_repo.get_job(job.job_id) is None


@pytest.mark.asyncio
async def test_concurrent_queries_do_not_raise_interface_error(job_repo, obs_repo):
    """R-W7.2/R1: before R-W1, self._conn wrapped one bare asyncpg.Connection
    per repo — a second query starting before the first returned raised
    `asyncpg.exceptions.InterfaceError: cannot perform operation: another
    operation is in progress`. This is what made R1 CRITICAL rather than
    theoretical: one process-wide AppContainer serves every FastAPI request
    plus three always-running background tasks against the same repo
    instances, so concurrent access wasn't an edge case, it was the normal
    case. self._conn is now pool-backed (PostgresConnection in
    postgres_conn.py) — every execute/fetchrow/fetch acquires its own pool
    connection per call, so concurrent callers on the same repo instance no
    longer contend for one connection object.

    Each repo class constructs its own pool independently (not one pool
    shared across all five repos — nothing in this codebase needs that),
    so the meaningful concurrency test is many simultaneous calls against
    one repo instance, run across two different repos in the same gather
    to also confirm nothing about one repo's pool interferes with another's.
    """
    job_ids = [_unique("job") for _ in range(20)]
    for job_id in job_ids:
        await job_repo.create_job(Job(job_id=job_id, url="https://example.com/listing"))

    domain = _unique("domain")
    results = await asyncio.gather(
        *(job_repo.get_job(job_id) for job_id in job_ids),
        *(obs_repo.get_or_create_profile(domain) for _ in range(10)),
        return_exceptions=True,
    )

    exceptions = [r for r in results if isinstance(r, BaseException)]
    assert exceptions == [], f"concurrent queries raised: {exceptions!r}"
    assert all(r is not None for r in results[: len(job_ids)])
