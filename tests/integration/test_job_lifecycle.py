# Integration test for the durable job lifecycle.
# Exercises the SqliteJobRepository adapter and Job state machine.

import pytest
import os
from datetime import datetime, timezone
from typing import Optional
from src.infrastructure.repositories.job_repository import SqliteJobRepository
from src.domain.models import Job, JobAttempt, JobState


@pytest.mark.asyncio
async def test_job_lifecycle_create_get():
    """Job can be created and retrieved."""
    repo = SqliteJobRepository(db_path="test_jobs.db")
    await repo.initialize()
    try:
        job = Job(
            job_id="test-job-1",
            url="https://example.com",
            target_site="universal",
        )
        created = await repo.create_job(job)
        assert created.job_id == "test-job-1"
        assert created.state == JobState.QUEUED

        fetched = await repo.get_job("test-job-1")
        assert fetched is not None
        assert fetched.job_id == "test-job-1"
        assert fetched.url == "https://example.com"
        assert fetched.state == JobState.QUEUED
    finally:
        await _cleanup_db("test_jobs.db", repo)


@pytest.mark.asyncio
async def test_job_state_transition():
    """Job can transition through valid states: QUEUED -> RUNNING -> SUCCEEDED."""
    repo = SqliteJobRepository(db_path="test_jobs.db")
    await repo.initialize()
    try:
        await repo.create_job(Job(
            job_id="test-transition", url="https://example.com",
        ))

        # QUEUED -> RUNNING
        updated = await repo.update_job_state("test-transition", JobState.RUNNING, expected_version=1)
        assert updated is not None
        assert updated.state == JobState.RUNNING
        assert updated.version == 2

        # RUNNING -> SUCCEEDED
        updated = await repo.update_job_state("test-transition", JobState.SUCCEEDED, expected_version=2)
        assert updated.state == JobState.SUCCEEDED

        # Verify final state persisted
        fetched = await repo.get_job("test-transition")
        assert fetched.state == JobState.SUCCEEDED
    finally:
        await _cleanup_db("test_jobs.db", repo)


@pytest.mark.asyncio
async def test_job_state_invalid_transition():
    """Invalid state transitions raise ValueError."""
    repo = SqliteJobRepository(db_path="test_jobs.db")
    await repo.initialize()
    try:
        await repo.create_job(Job(
            job_id="test-invalid", url="https://example.com",
        ))

        # SUCCEEDED is terminal — can't go back to RUNNING
        await repo.update_job_state("test-invalid", JobState.SUCCEEDED, expected_version=1)
        with pytest.raises(ValueError, match="Invalid state transition"):
            # Use the model-level guard (repository update bypasses the guard,
            # so this test uses Job.transition_to directly)
            job = await repo.get_job("test-invalid")
            job.transition_to(JobState.RUNNING)
    finally:
        await _cleanup_db("test_jobs.db", repo)


@pytest.mark.asyncio
async def test_job_not_found():
    """Querying a non-existent job returns None."""
    repo = SqliteJobRepository(db_path="test_jobs.db")
    await repo.initialize()
    try:
        result = await repo.get_job("nonexistent")
        assert result is None
    finally:
        await _cleanup_db("test_jobs.db", repo)


@pytest.mark.asyncio
async def test_job_record_count():
    """record_count can be updated independently of state."""
    repo = SqliteJobRepository(db_path="test_jobs.db")
    await repo.initialize()
    try:
        await repo.create_job(Job(
            job_id="test-count", url="https://example.com",
        ))
        await repo.update_job_record_count("test-count", 42)
        job = await repo.get_job("test-count")
        assert job.record_count == 42
    finally:
        await _cleanup_db("test_jobs.db", repo)


@pytest.mark.asyncio
async def test_create_attempt():
    """Job attempts can be created, updated, and listed."""
    repo = SqliteJobRepository(db_path="test_jobs.db")
    await repo.initialize()
    try:
        await repo.create_job(Job(
            job_id="test-attempt", url="https://example.com",
        ))
        attempt = JobAttempt(
            attempt_id="att-1",
            job_id="test-attempt",
            worker_id="worker-1",
        )
        created = await repo.create_attempt(attempt)
        assert created.attempt_id == "att-1"

        # Complete the attempt
        updated = await repo.update_attempt(
            "att-1",
            state=JobState.SUCCEEDED,
            finished_at=datetime.now(tz=timezone.utc).isoformat(),
        )
        assert updated.state == JobState.SUCCEEDED

        # List attempts
        attempts = await repo.get_attempts("test-attempt")
        assert len(attempts) == 1
        assert attempts[0].attempt_id == "att-1"
    finally:
        await _cleanup_db("test_jobs.db", repo)


async def _cleanup_db(db_path: str, repo: Optional[SqliteJobRepository] = None):
    """Remove test database and WAL/SHM files."""
    if repo:
        await repo.close()
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.exists(path):
            try:
                os.remove(path)
            except PermissionError:
                pass  # may already be deleted by another test
