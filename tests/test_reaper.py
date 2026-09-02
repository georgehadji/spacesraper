# JobReaper: fails stale RUNNING jobs, purges expired soft-deleted ones.
# P0 gate item 6 (docs/plans/2026-08-13-capability-enhancement-plan.md).

import pytest

from src.application.reaper import STALE_ERROR_MESSAGE, JobReaper
from src.domain.models import Job, JobState
from src.infrastructure.repositories.job_repository import SqliteJobRepository


class FakeJobRepo:
    def __init__(self, stale_jobs: list[Job], purge_count: int = 0):
        self._stale_jobs = stale_jobs
        self._purge_count = purge_count
        self.updated: list[tuple[str, JobState, str | None]] = []
        self.purge_called_with: int | None = None

    async def find_stale_jobs(self, stale_seconds: int, limit: int = 50) -> list[Job]:
        return self._stale_jobs

    async def update_job_state(self, job_id, new_state, *, expected_version, error_message=None):
        self.updated.append((job_id, new_state, error_message))
        return Job(job_id=job_id, url="https://example.com", state=new_state, version=expected_version + 1)

    async def purge_expired_jobs(self, retention_days: int = 90) -> int:
        self.purge_called_with = retention_days
        return self._purge_count


@pytest.mark.asyncio
async def test_reap_once_fails_stale_running_jobs():
    stale = Job(job_id="j1", url="https://example.com", state=JobState.RUNNING, version=3)
    repo = FakeJobRepo(stale_jobs=[stale])
    reaper = JobReaper(job_repo=repo)

    count = await reaper.reap_once(stale_seconds=120)

    assert count == 1
    assert repo.updated == [("j1", JobState.FAILED, STALE_ERROR_MESSAGE)]


@pytest.mark.asyncio
async def test_reap_once_no_stale_jobs_is_a_noop():
    repo = FakeJobRepo(stale_jobs=[])
    reaper = JobReaper(job_repo=repo)

    count = await reaper.reap_once()

    assert count == 0
    assert repo.updated == []


@pytest.mark.asyncio
async def test_purge_once_delegates_retention_days():
    repo = FakeJobRepo(stale_jobs=[], purge_count=5)
    reaper = JobReaper(job_repo=repo)

    purged = await reaper.purge_once(retention_days=30)

    assert purged == 5
    assert repo.purge_called_with == 30


@pytest.mark.asyncio
async def test_purge_once_actually_deletes_rows_end_to_end():
    """R-W7.3: the tests above prove JobReaper delegates to
    purge_expired_jobs correctly, but FakeJobRepo's purge_expired_jobs is
    itself a fake that just returns a canned count — it can't catch
    purge_expired_jobs being missing or broken on a real adapter, which is
    exactly how R2 (purge_expired_jobs undeclared/unimplemented on every
    backend) went unnoticed while JobReaper called it in production daily.
    This runs against the real SqliteJobRepository, soft-deletes a job with
    retention_days=0 (immediately eligible — no clock mocking needed, any
    non-negative elapsed time satisfies `>= timedelta(days=0)`), and
    confirms the row is actually gone afterward, not just that some method
    was called."""
    repo = SqliteJobRepository(db_path=":memory:")
    await repo.initialize()
    try:
        job = Job(job_id="reap-purge-1", url="https://example.com", retention_days=0)
        await repo.create_job(job)
        await repo.update_job_state(job.job_id, JobState.SUCCEEDED, expected_version=1)
        deleted = await repo.soft_delete_job(job.job_id)
        assert deleted is not None and deleted.state == JobState.DELETED

        reaper = JobReaper(job_repo=repo)
        purged = await reaper.purge_once(retention_days=90)  # job's own retention_days=0 overrides this

        assert purged == 1
        assert await repo.get_job(job.job_id) is None
    finally:
        await repo.close()
