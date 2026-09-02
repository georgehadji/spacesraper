# Job reaper — periodically fails RUNNING jobs whose worker went silent, and
# hard-deletes soft-deleted jobs past their retention window.
# Design: docs/plans/2026-08-13-capability-enhancement-plan.md P0 item 6,
# ARCHITECTURE_REMEDIATION_v3.md ("Stale Job Reaper").

import asyncio
import logging
import time

from src.domain.models import JobState
from src.domain.ports import JobRepository

logger = logging.getLogger("Spacescraper.Reaper")

DEFAULT_REAP_INTERVAL_S = 60
DEFAULT_STALE_SECONDS = 120
DEFAULT_PURGE_INTERVAL_S = 86400
DEFAULT_RETENTION_DAYS = 90
STALE_ERROR_MESSAGE = "Job timed out — worker unresponsive"


class JobReaper:
    """Fails stale RUNNING jobs and purges expired soft-deleted jobs."""

    def __init__(self, job_repo: JobRepository):
        self.job_repo = job_repo

    async def reap_once(self, stale_seconds: int = DEFAULT_STALE_SECONDS, limit: int = 50) -> int:
        stale_jobs = await self.job_repo.find_stale_jobs(stale_seconds, limit=limit)
        reaped = 0
        for job in stale_jobs:
            result = await self.job_repo.update_job_state(
                job.job_id, JobState.FAILED,
                expected_version=job.version, error_message=STALE_ERROR_MESSAGE,
            )
            if result is not None:
                reaped += 1
        if reaped:
            logger.warning("JobReaper: failed %d stale job(s)", reaped)
        return reaped

    async def purge_once(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
        purged = await self.job_repo.purge_expired_jobs(retention_days)
        if purged:
            logger.info("JobReaper: purged %d expired job(s)", purged)
        return purged

    async def run_forever(
        self,
        reap_interval: int = DEFAULT_REAP_INTERVAL_S,
        purge_interval: int = DEFAULT_PURGE_INTERVAL_S,
        stale_seconds: int = DEFAULT_STALE_SECONDS,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        last_purge = time.monotonic()
        while True:
            try:
                await self.reap_once(stale_seconds=stale_seconds)
                if time.monotonic() - last_purge >= purge_interval:
                    await self.purge_once(retention_days=retention_days)
                    last_purge = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("JobReaper: tick failed")
            await asyncio.sleep(reap_interval)
