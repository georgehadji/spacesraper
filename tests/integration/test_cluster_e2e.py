# End-to-end smoke test for the Spacescraper cluster.
# Drives a job through the real queue -> scraper -> queue -> processor -> storage
# path with only the browser engine stubbed, so wiring regressions surface here.

import asyncio
import json

import pytest

from src.domain.models import ScrapeJob, RawScrapePayload, JobState, Job
from src.infrastructure.queues.valkey_worker import ValkeyQueueWorker
from src.infrastructure.repositories.job_repository import SqliteJobRepository
from src.infrastructure.storage.sqlite_tracker import SqliteTracker
from src.infrastructure.repositories.record_repository import SqliteRecordRepository
from src.application.post_processor import IntelligencePostProcessor

import worker_scraper
import worker_processor


SAMPLE_HTML = """
<html><body>
  <script type="application/ld+json">
  {"@context":"https://schema.org","@type":"Product","name":"Spacescraper Pro","description":"Test listing"}
  </script>
  <article><h2>Heavy Equipment Maintenance</h2><p>Ministry of Defense tender</p></article>
</body></html>
"""


class _StubEngine:
    """Stands in for the Playwright engine; returns fixed HTML with no browser."""

    persona = None

    def __init__(self, *args, **kwargs):
        pass

    async def start(self, persona_id=None):
        return None

    async def crawl(self, url):
        return RawScrapePayload(
            job_id="stub",
            target_site="universal",
            url=url,
            status_code=200,
            html_content=SAMPLE_HTML,
            json_payloads=[],
        )

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_cluster_processes_job_end_to_end(tmp_path, monkeypatch):
    """A seeded job must reach the processor and land as a persisted record."""
    # Force the in-memory backend so the test is hermetic and never writes to a
    # developer's live Valkey. A shared instance is the whole point here: separate
    # fallback clients each own a private store and never exchange messages.
    queue = ValkeyQueueWorker()
    queue._setup_mock()
    await queue.connect()
    assert queue._is_mock

    monkeypatch.setattr(worker_scraper, "ScraperEngine", _StubEngine)

    job_repo = SqliteJobRepository(db_path=str(tmp_path / "jobs.db"))
    await job_repo.initialize()

    scraper = worker_scraper.ScraperWorkerService(job_repo=job_repo, queue=queue)
    await scraper.obs_repo.initialize()

    processor = worker_processor.ProcessorWorkerService(queue=queue)
    processor.intel_tracker = SqliteTracker(db_path=str(tmp_path / "intel.db"))
    await processor.intel_tracker.initialize()
    processor.post_processor = IntelligencePostProcessor(intel_tracker=processor.intel_tracker)
    processor.record_repo = SqliteRecordRepository(db_path=str(tmp_path / "records.db"))
    await processor.record_repo.initialize()
    processor.job_repo = job_repo

    # Durable record so the scraper's state machine has something to transition.
    await job_repo.create_job(Job(job_id="e2e-1", url="https://example.com/listing"))

    # 1. Seed
    await queue.push_job(
        "jobs_queue",
        ScrapeJob(job_id="e2e-1", url="https://example.com/listing", target_site="universal"),
    )

    # 2. Scrape: pull the seeded job off the queue exactly as poll_jobs would.
    popped = await queue.valkey.blpop("jobs_queue", timeout=2)
    assert popped is not None, "Seeded job never reached the shared queue"
    await scraper.process_job(ScrapeJob(**json.loads(popped[1])))

    stored = await job_repo.get_job("e2e-1")
    assert stored.state == JobState.SUCCEEDED, f"Job state stuck at {stored.state}"

    # 3. Process: the scraper's payload must be visible on the same queue.
    raw = await queue.valkey.blpop("raw_data_queue", timeout=2)
    assert raw is not None, "Scraper payload never reached the processor queue"
    await processor.process_payload(RawScrapePayload(**json.loads(raw[1])))

    # 4. Persisted output
    records, _ = await processor.record_repo.list_records("e2e-1")
    assert records, "No ExtractedRecord persisted for the job"

    await job_repo.close()
    await processor.intel_tracker.close()
    await processor.record_repo.close()
    await scraper.obs_repo.close()
    await queue.close()


@pytest.mark.asyncio
async def test_turbo_path_releases_rate_limiter_slot():
    """The Turbo Mode early return must not leak the per-domain concurrency slot."""
    service = worker_scraper.ScraperWorkerService()
    domain = "api.example.com"
    url = "https://api.example.com/feed"
    service.hybrid_domains.add(domain)
    service.rate_limiter.set_budget(domain, 1)

    async def _empty_turbo(job):
        return RawScrapePayload(
            job_id=job.job_id, target_site=job.target_site,
            url=job.url, status_code=200, json_payloads=[],
        )

    service._perform_turbo_scrape = _empty_turbo

    for _ in range(3):
        await asyncio.wait_for(
            service.process_job(ScrapeJob(job_id="t-1", url=url, target_site="universal")),
            timeout=5,
        )

    # A leaked slot would have made the second call block until its 60s deadline.
    assert service.rate_limiter._get_semaphore(domain)._value == 1
