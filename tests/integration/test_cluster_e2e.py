# End-to-end smoke test for the Spacescraper cluster.
# Drives a job through the real stream -> scraper -> stream -> processor -> storage
# -> discovery_stream -> reporter -> artifact path, with only the browser engine
# stubbed, so wiring regressions surface here.

import asyncio
import glob
import json
import os

import pytest

import worker_processor
import worker_reporter
import worker_scraper
from src.domain.models import Job, JobState, MessageType, RawScrapePayload, ScrapeJob
from src.infrastructure.exports.report_generator import ReportGenerator
from src.infrastructure.queues.stream_queue import ValkeyStreamQueue, make_message
from src.infrastructure.repositories.job_repository import SqliteJobRepository
from src.infrastructure.repositories.overlay_repository import SqliteOverlayRepository
from src.infrastructure.repositories.record_repository import SqliteRecordRepository
from src.infrastructure.storage.sqlite_tracker import SqliteTracker

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

    async def crawl(self, url, network_idle=False, wait_selector=None):
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


async def _pop_one(stream_queue: ValkeyStreamQueue, stream: str, group: str = "e2e"):
    """Read a single pending entry off a stream, ack it, and return the QueueMessage.
    Mirrors the read half of ValkeyStreamQueue.consume() without the infinite loop,
    since this test drives the pipeline step-by-step rather than via the long-poll loop."""
    from src.domain.models import QueueMessage

    await stream_queue._ensure_group(stream, group)
    results = await stream_queue._valkey.xreadgroup(
        group, "e2e-reader", {stream: ">"}, count=1, block=2000,
    )
    assert results, f"No message on stream {stream!r}"
    _, entries = results[0]
    entry_id, data = entries[0]
    message = QueueMessage(**json.loads(data["payload"]))
    await stream_queue._valkey.xack(stream, group, entry_id)
    return message


@pytest.mark.asyncio
async def test_cluster_processes_job_end_to_end(tmp_path, monkeypatch):
    """A seeded job must reach the processor and land as a persisted record."""
    # Force the in-memory backend so the test is hermetic and never writes to a
    # developer's live Valkey. A shared instance is the whole point here: separate
    # fallback clients each own a private store and never exchange messages.
    stream_queue = ValkeyStreamQueue()
    await stream_queue._setup_mock()
    assert stream_queue._is_mock

    monkeypatch.setattr(worker_scraper, "ScraperEngine", _StubEngine)

    job_repo = SqliteJobRepository(db_path=str(tmp_path / "jobs.db"))
    await job_repo.initialize()

    scraper = worker_scraper.ScraperWorkerService(
        job_repo=job_repo, stream_queue=stream_queue, robots_gate=_AllowAllRobotsGate(),
    )
    await scraper.obs_repo.initialize()

    # Constructor injection (W4.4): every collaborator is wired in at __init__
    # time, so post_processor/strategies are built from the real instances
    # directly — no post-construct attribute-swap-then-rebuild needed.
    intel_tracker = SqliteTracker(db_path=str(tmp_path / "intel.db"))
    await intel_tracker.initialize()
    record_repo = SqliteRecordRepository(db_path=str(tmp_path / "records.db"))
    await record_repo.initialize()
    overlay_repo = SqliteOverlayRepository(db_path=str(tmp_path / "jobs.db"))
    await overlay_repo.initialize()

    processor = worker_processor.ProcessorWorkerService(
        stream_queue=stream_queue,
        job_repo=job_repo,
        record_repo=record_repo,
        overlay_repo=overlay_repo,
        intel_tracker=intel_tracker,
    )

    # Durable record so the scraper's state machine has something to transition.
    await job_repo.create_job(Job(job_id="e2e-1", url="https://example.com/listing"))

    # 1. Seed
    await stream_queue.push(
        "jobs_stream",
        make_message(
            MessageType.SCRAPE_JOB,
            ScrapeJob(job_id="e2e-1", url="https://example.com/listing", target_site="universal")
            .model_dump(mode="json"),
            root_job_id="e2e-1",
        ),
    )

    # 2. Scrape: pull the seeded job off the stream exactly as consume() would.
    job_message = await _pop_one(stream_queue, "jobs_stream", "scrapers")
    await scraper.process_stream_message(job_message)

    stored = await job_repo.get_job("e2e-1")
    assert stored.state == JobState.SUCCEEDED, f"Job state stuck at {stored.state}"

    # 3. Process: the scraper's payload must be visible on the same stream.
    raw_message = await _pop_one(stream_queue, "raw_data_stream", "processors")
    await processor.process_stream_message(raw_message)

    # 4. Persisted output
    records, _ = await processor.record_repo.list_records("e2e-1")
    assert records, "No ExtractedRecord persisted for the job"

    # 5. Reporter: the NEW/UPDATED records from step 3 must have produced a
    # discovery signal (C3), and the reporter must turn it into a real artifact
    # file (W3.4). Redirect both artifact writers to tmp_path so this test
    # never touches the repo's real exports/ directory.
    reporter = worker_reporter.ReporterWorkerService()
    reporter.report_gen = ReportGenerator(export_dir=str(tmp_path / "exports"))
    real_write_artifacts = worker_reporter.write_artifacts

    async def _write_artifacts_to_tmp(records, name_prefix, formats):
        return await real_write_artifacts(
            records, target_dir=str(tmp_path / "exports"), name_prefix=name_prefix, formats=formats
        )

    monkeypatch.setattr(worker_reporter, "write_artifacts", _write_artifacts_to_tmp)

    discovery_message = await _pop_one(stream_queue, "discovery_stream", "reporters")
    await reporter.process_stream_message(discovery_message)

    artifact_files = glob.glob(os.path.join(str(tmp_path / "exports"), "*"))
    assert artifact_files, "Reporter did not write any artifact file for the discovery event"

    await job_repo.close()
    await processor.intel_tracker.close()
    await processor.record_repo.close()
    await processor.overlay_repo.close()
    await scraper.obs_repo.close()
    await stream_queue.close()


class _AllowAllRobotsGate:
    """P2's fail-closed default would otherwise reject this fake domain
    before turbo mode ever runs, defeating this test's premise."""

    async def is_allowed(self, url):
        return True

    async def crawl_delay_seconds(self, url):
        return None


@pytest.mark.asyncio
async def test_turbo_path_releases_rate_limiter_slot():
    """The Turbo Mode early return must not leak the per-domain concurrency slot."""
    service = worker_scraper.ScraperWorkerService(robots_gate=_AllowAllRobotsGate())
    await service.stream_queue._setup_mock()
    domain = "api.example.com"
    url = "https://api.example.com/feed"
    service.domain_endpoints[domain] = [{"url": url, "content_type": "application/json"}]
    service.rate_limiter.set_budget(domain, 1)

    async def _turbo_hit(job, endpoints):
        return RawScrapePayload(
            job_id=job.job_id, target_site=job.target_site,
            url=job.url, status_code=200,
            json_payloads=[{"url": url, "data": {"ok": True}}],
        )

    service._perform_turbo_scrape = _turbo_hit

    for _ in range(3):
        await asyncio.wait_for(
            service.process_job(ScrapeJob(job_id="t-1", url=url, target_site="universal")),
            timeout=5,
        )

    # A leaked slot would have made the second call block until its 60s deadline.
    assert service.rate_limiter._get_semaphore(domain)._value == 1
