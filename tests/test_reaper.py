# JobReaper: fails stale RUNNING jobs, purges expired soft-deleted ones.
# P0 gate item 6 (docs/plans/2026-08-13-capability-enhancement-plan.md).

import pytest

from src.application.reaper import STALE_ERROR_MESSAGE, JobReaper
from src.domain.models import Job, JobState


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
