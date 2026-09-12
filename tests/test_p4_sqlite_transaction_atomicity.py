# P4 D20: SqliteJobRepository.transaction() is not atomic.
#
# transaction() yields the process-wide shared connection, and every other
# write method commits that same connection — so an unrelated concurrent write
# landing mid-block committed the caller's half-written unit of work, leaving
# the rollback nothing to undo.

import asyncio
import contextlib

import pytest

from src.domain.models import Job, JobState
from src.infrastructure.repositories.job_repository import SqliteJobRepository


async def _repo(tmp_path) -> SqliteJobRepository:
    repo = SqliteJobRepository(db_path=str(tmp_path / "jobs.db"))
    await repo.initialize()
    return repo


def _job(job_id: str) -> Job:
    return Job(job_id=job_id, url="https://example.com", target_site="example.com")


@pytest.mark.asyncio
async def test_a_concurrent_write_cannot_commit_a_half_written_transaction(tmp_path):
    """The defect, stated as the property it breaks: a unit of work that raises
    leaves nothing behind."""
    repo = await _repo(tmp_path)
    try:
        released = asyncio.Event()

        async def concurrent_heartbeat() -> None:
            await released.wait()
            await repo.heartbeat("some-other-job")

        beat = asyncio.create_task(concurrent_heartbeat())

        with pytest.raises(RuntimeError, match="unit of work failed"):
            async with repo.transaction() as tx:
                await repo.create_job(_job("j-atomic"), conn=tx)
                released.set()
                # Give the heartbeat a real chance to land — aiosqlite runs it
                # on a background thread, so yielding with sleep(0) is not
                # enough for it to complete. Before the fix it finishes here
                # and its commit takes the pending INSERT with it; after, it
                # blocks on the write lock and this times out.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(beat), timeout=1.0)
                raise RuntimeError("unit of work failed")

        await beat

        assert await repo.get_job("j-atomic") is None, (
            "a concurrent write committed the rolled-back job"
        )
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_a_clean_transaction_still_commits(tmp_path):
    """Control: serialising writes must not stop the happy path committing."""
    repo = await _repo(tmp_path)
    try:
        async with repo.transaction() as tx:
            await repo.create_job(_job("j-ok"), conn=tx)

        stored = await repo.get_job("j-ok")
        assert stored is not None
        assert stored.state is JobState.QUEUED
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_ordinary_writes_still_work_while_no_transaction_is_open(tmp_path):
    """Control: the lock is uncontended in the common case — a plain write
    outside any transaction() block behaves exactly as before."""
    repo = await _repo(tmp_path)
    try:
        await repo.create_job(_job("j-plain"))
        await repo.heartbeat("j-plain")

        stored = await repo.get_job("j-plain")
        assert stored is not None
        assert stored.last_heartbeat_at is not None
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_concurrent_plain_writes_do_not_deadlock(tmp_path):
    """Control: serialisation must not turn independent writes into a
    deadlock."""
    repo = await _repo(tmp_path)
    try:
        await asyncio.gather(*(repo.create_job(_job(f"j-{n}")) for n in range(5)))
        stored = [await repo.get_job(f"j-{n}") for n in range(5)]
        assert all(job is not None for job in stored)
    finally:
        await repo.close()
