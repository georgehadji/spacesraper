import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.domain.models import ScrapeJob, RawScrapePayload
from worker_scraper import ScraperWorkerService


def make_job(url="https://api.example.com/opportunities"):
    return ScrapeJob(
        job_id="test-job-1",
        url=url,
        target_site="test_source"
    )


def make_turbo_payload(job, json_payloads=None):
    return RawScrapePayload(
        job_id=job.job_id,
        target_site=job.target_site,
        url=job.url,
        status_code=200,
        json_payloads=json_payloads or []
    )


@pytest.mark.asyncio
async def test_turbo_miss_counter_increments_on_empty_payload():
    """Empty JSON payload from turbo scrape must increment the miss counter."""
    service = ScraperWorkerService()
    domain = "api.example.com"
    service.hybrid_registry["https://api.example.com/opportunities"] = True
    service.hybrid_domains.add(domain)

    job = make_job()
    empty_payload = make_turbo_payload(job, json_payloads=[])

    with patch.object(service, "_perform_turbo_scrape", return_value=empty_payload), \
         patch.object(service.queue, "push_raw_payload", new_callable=AsyncMock), \
         patch("worker_scraper.metrics_tracker") as mock_metrics:
        mock_metrics.record_job_status = AsyncMock()
        mock_metrics.increment = AsyncMock()
        await service.process_job(job)

    assert service._turbo_miss_counts.get(domain, 0) == 1


@pytest.mark.asyncio
async def test_turbo_domain_demoted_after_threshold_misses():
    """After TURBO_MISS_THRESHOLD consecutive empty yields, domain must be evicted."""
    service = ScraperWorkerService()
    domain = "api.example.com"
    url = "https://api.example.com/opportunities"
    service.hybrid_registry[url] = True
    service.hybrid_domains.add(domain)
    service._turbo_miss_counts[domain] = service.TURBO_MISS_THRESHOLD - 1

    job = make_job(url=url)
    empty_payload = make_turbo_payload(job, json_payloads=[])

    with patch.object(service, "_perform_turbo_scrape", return_value=empty_payload), \
         patch.object(service.queue, "push_raw_payload", new_callable=AsyncMock), \
         patch("worker_scraper.metrics_tracker") as mock_metrics:
        mock_metrics.record_job_status = AsyncMock()
        mock_metrics.increment = AsyncMock()
        await service.process_job(job)

    assert url not in service.hybrid_registry
    assert domain not in service.hybrid_domains
    assert domain not in service._turbo_miss_counts
    mock_metrics.increment.assert_any_call("turbo_yield_failure")


@pytest.mark.asyncio
async def test_turbo_miss_counter_resets_on_successful_yield():
    """Non-empty JSON payload must reset the miss counter for that domain."""
    service = ScraperWorkerService()
    domain = "api.example.com"
    url = "https://api.example.com/opportunities"
    service.hybrid_registry[url] = True
    service.hybrid_domains.add(domain)
    service._turbo_miss_counts[domain] = 2  # pre-populated misses

    job = make_job(url=url)
    good_payload = make_turbo_payload(job, json_payloads=[{"url": url, "data": {"results": [1, 2]}}])

    with patch.object(service, "_perform_turbo_scrape", return_value=good_payload), \
         patch.object(service.queue, "push_raw_payload", new_callable=AsyncMock), \
         patch("worker_scraper.metrics_tracker") as mock_metrics:
        mock_metrics.record_job_status = AsyncMock()
        mock_metrics.increment = AsyncMock()
        await service.process_job(job)

    assert domain not in service._turbo_miss_counts
    assert url in service.hybrid_registry  # still promoted


@pytest.mark.asyncio
async def test_empty_turbo_payload_not_forwarded_to_queue():
    """Empty turbo payload must NOT be pushed to raw_data_queue."""
    service = ScraperWorkerService()
    domain = "api.example.com"
    url = "https://api.example.com/opportunities"
    service.hybrid_registry[url] = True
    service.hybrid_domains.add(domain)

    job = make_job(url=url)
    empty_payload = make_turbo_payload(job, json_payloads=[])
    push_calls = []

    async def mock_push(queue_name, payload):
        push_calls.append(queue_name)

    with patch.object(service, "_perform_turbo_scrape", return_value=empty_payload), \
         patch.object(service.queue, "push_raw_payload", side_effect=mock_push), \
         patch("worker_scraper.metrics_tracker") as mock_metrics:
        mock_metrics.record_job_status = AsyncMock()
        mock_metrics.increment = AsyncMock()
        await service.process_job(job)

    assert len(push_calls) == 0, "Empty turbo payload must not be forwarded to raw_data_queue"


@pytest.mark.asyncio
async def test_empty_turbo_payload_recorded_as_failure():
    """Empty turbo payload must be recorded as job failure, not success."""
    service = ScraperWorkerService()
    domain = "api.example.com"
    url = "https://api.example.com/opportunities"
    service.hybrid_registry[url] = True
    service.hybrid_domains.add(domain)

    job = make_job(url=url)
    empty_payload = make_turbo_payload(job, json_payloads=[])

    recorded_statuses = []

    async def mock_record_status(success):
        recorded_statuses.append(success)

    with patch.object(service, "_perform_turbo_scrape", return_value=empty_payload), \
         patch.object(service.queue, "push_raw_payload", new_callable=AsyncMock), \
         patch("worker_scraper.metrics_tracker") as mock_metrics:
        mock_metrics.record_job_status = mock_record_status
        mock_metrics.increment = AsyncMock()
        await service.process_job(job)

    assert len(recorded_statuses) == 1
    assert recorded_statuses[0] is False, "Empty turbo payload must record success=False"


def test_turbo_yield_failure_in_metric_keys():
    """turbo_yield_failure must be a tracked metric key."""
    from src.infrastructure.monitoring.observability import ObservabilityMetrics
    m = ObservabilityMetrics()
    assert "turbo_yield_failure" in m.metric_keys
