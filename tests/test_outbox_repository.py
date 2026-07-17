# Tests for SqliteOutboxRepository.

import os
import pytest
from src.infrastructure.repositories.outbox_repository import SqliteOutboxRepository
from src.domain.models import OutboxEvent, OutboxStatus


@pytest.mark.asyncio
async def test_create_and_get_event():
    repo = await _make_repo()
    try:
        event = OutboxEvent(
            event_id="ev-1",
            aggregate_type="job",
            aggregate_id="job-1",
            event_type="job.submitted",
            payload={"url": "https://example.com"},
        )
        await repo.create_event(event)
        fetched = await repo.get_event("ev-1")
        assert fetched is not None
        assert fetched.event_type == "job.submitted"
        assert fetched.status == OutboxStatus.PENDING
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_get_pending_events():
    repo = await _make_repo()
    try:
        for i in range(3):
            ev = OutboxEvent(
                event_id=f"ev-p{i}",
                aggregate_type="job",
                aggregate_id=f"job-{i}",
                event_type="job.submitted",
            )
            await repo.create_event(ev)

        pending = await repo.get_pending_events(limit=10)
        assert len(pending) == 3
        assert all(e.status == OutboxStatus.PENDING for e in pending)
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_mark_delivered():
    repo = await _make_repo()
    try:
        ev = OutboxEvent(
            event_id="ev-deliver",
            aggregate_type="job",
            aggregate_id="job-d",
            event_type="job.completed",
        )
        await repo.create_event(ev)
        await repo.mark_delivered("ev-deliver")

        fetched = await repo.get_event("ev-deliver")
        assert fetched.status == OutboxStatus.DELIVERED
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_mark_failed_with_retry():
    """mark_failed increments retry_count and stays PENDING until max_retries."""
    repo = await _make_repo()
    try:
        ev = OutboxEvent(
            event_id="ev-fail",
            aggregate_type="job",
            aggregate_id="job-f",
            event_type="job.submitted",
            max_retries=3,
        )
        await repo.create_event(ev)

        await repo.mark_failed("ev-fail", "conn_error")
        fetched = await repo.get_event("ev-fail")
        assert fetched.retry_count == 1
        assert fetched.status == OutboxStatus.PENDING  # not yet exhausted
        assert "conn_error" in fetched.last_error

        # Exhaust retries
        await repo.mark_failed("ev-fail", "final_error")
        await repo.mark_failed("ev-fail", "exhausted")
        fetched = await repo.get_event("ev-fail")
        assert fetched.status == OutboxStatus.FAILED
        assert fetched.retry_count == 3
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_pending_count():
    repo = await _make_repo()
    try:
        ev = OutboxEvent(
            event_id="ev-count",
            aggregate_type="job",
            aggregate_id="job-c",
            event_type="job.submitted",
        )
        await repo.create_event(ev)
        count = await repo.get_pending_count()
        assert count == 1

        await repo.mark_delivered("ev-count")
        count = await repo.get_pending_count()
        assert count == 0
    finally:
        await _cleanup(repo)


async def _make_repo() -> SqliteOutboxRepository:
    repo = SqliteOutboxRepository(db_path="test_outbox.db")
    await repo.initialize()
    return repo


async def _cleanup(repo: SqliteOutboxRepository):
    await repo.close()
    for suffix in ("", "-wal", "-shm"):
        path = f"test_outbox.db{suffix}"
        if os.path.exists(path):
            os.remove(path)
