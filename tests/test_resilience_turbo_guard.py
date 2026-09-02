from unittest.mock import AsyncMock, patch

import pytest

from src.domain.models import RawScrapePayload, ScrapeJob
from worker_scraper import ScraperWorkerService

ENDPOINT_URL = "https://api.example.com/v1/opportunities.json"


class _AllowAllRobotsGate:
    """These tests exercise turbo-fallback behavior, not P2 politeness — a
    real robots.txt fetch for a fake domain would fail closed and short-
    circuit before turbo logic ever runs."""

    async def is_allowed(self, url):
        return True

    async def crawl_delay_seconds(self, url):
        return None


def make_job(url="https://api.example.com/opportunities"):
    return ScrapeJob(
        job_id="test-job-1",
        url=url,
        target_site="test_source"
    )


def make_service(**kwargs):
    kwargs.setdefault("robots_gate", _AllowAllRobotsGate())
    return ScraperWorkerService(**kwargs)


def make_turbo_payload(job, json_payloads=None):
    return RawScrapePayload(
        job_id=job.job_id,
        target_site=job.target_site,
        url=job.url,
        status_code=200,
        json_payloads=json_payloads or []
    )


class _BrowserFallbackStub:
    """Stands in for ScraperEngine on the in-job browser fallback path."""

    persona = None

    def __init__(self, *args, **kwargs):
        pass

    async def start(self, persona_id=None, proxy=None):
        return None

    async def crawl(self, url, network_idle=False, wait_selector=None):
        return RawScrapePayload(
            job_id="fallback",
            target_site="universal",
            url=url,
            status_code=200,
            html_content="<html>fallback</html>",
            json_payloads=[],
        )

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_turbo_miss_counter_increments_and_falls_through_to_browser(monkeypatch):
    """
    A turbo endpoint replay that yields nothing must increment the miss
    counter and fall through to a browser fetch in the same job (S3) —
    it must NOT be recorded as JobState.FAILED.
    """
    service = make_service()
    domain = "api.example.com"
    service.domain_endpoints[domain] = [{"url": ENDPOINT_URL, "content_type": "application/json"}]
    monkeypatch.setattr("worker_scraper.ScraperEngine", _BrowserFallbackStub)

    job = make_job()
    empty_payload = make_turbo_payload(job, json_payloads=[])

    with patch.object(service, "_perform_turbo_scrape", return_value=empty_payload), \
         patch.object(service.stream_queue, "push", new_callable=AsyncMock), \
         patch("worker_scraper.metrics_tracker") as mock_metrics:
        mock_metrics.record_job_status = AsyncMock()
        mock_metrics.increment = AsyncMock()
        await service.process_job(job)

    assert service._turbo_miss_counts.get(domain, 0) == 1
    mock_metrics.increment.assert_any_call("turbo_endpoint_miss")
    # Falls through to the browser rather than failing the job outright.
    mock_metrics.record_job_status.assert_called_once_with(success=True)


@pytest.mark.asyncio
async def test_turbo_domain_demoted_after_threshold_misses(monkeypatch):
    """After TURBO_MISS_THRESHOLD consecutive empty yields, domain must be evicted."""
    service = make_service()
    domain = "api.example.com"
    service.domain_endpoints[domain] = [{"url": ENDPOINT_URL, "content_type": "application/json"}]
    service._turbo_miss_counts[domain] = service.TURBO_MISS_THRESHOLD - 1
    monkeypatch.setattr("worker_scraper.ScraperEngine", _BrowserFallbackStub)

    job = make_job()
    empty_payload = make_turbo_payload(job, json_payloads=[])

    with patch.object(service, "_perform_turbo_scrape", return_value=empty_payload), \
         patch.object(service.stream_queue, "push", new_callable=AsyncMock), \
         patch("worker_scraper.metrics_tracker") as mock_metrics:
        mock_metrics.record_job_status = AsyncMock()
        mock_metrics.increment = AsyncMock()
        await service.process_job(job)

    assert domain not in service.domain_endpoints
    assert domain not in service._turbo_miss_counts
    mock_metrics.increment.assert_any_call("turbo_yield_failure")


@pytest.mark.asyncio
async def test_turbo_miss_counter_resets_on_successful_yield():
    """Non-empty JSON payload must reset the miss counter and keep the domain promoted."""
    service = make_service()
    domain = "api.example.com"
    service.domain_endpoints[domain] = [{"url": ENDPOINT_URL, "content_type": "application/json"}]
    service._turbo_miss_counts[domain] = 2  # pre-populated misses

    job = make_job()
    good_payload = make_turbo_payload(job, json_payloads=[{"url": ENDPOINT_URL, "data": {"results": [1, 2]}}])

    with patch.object(service, "_perform_turbo_scrape", return_value=good_payload), \
         patch.object(service.stream_queue, "push", new_callable=AsyncMock), \
         patch("worker_scraper.metrics_tracker") as mock_metrics:
        mock_metrics.record_job_status = AsyncMock()
        mock_metrics.increment = AsyncMock()
        await service.process_job(job)

    assert domain not in service._turbo_miss_counts
    assert domain in service.domain_endpoints  # still promoted
    mock_metrics.increment.assert_any_call("turbo_endpoint_hit")


@pytest.mark.asyncio
async def test_turbo_miss_does_not_double_push_only_browser_fallback_pushes(monkeypatch):
    """
    The empty turbo payload itself must never reach raw_data_stream — only the
    browser fallback's payload, exactly once.
    """
    service = make_service()
    domain = "api.example.com"
    service.domain_endpoints[domain] = [{"url": ENDPOINT_URL, "content_type": "application/json"}]
    monkeypatch.setattr("worker_scraper.ScraperEngine", _BrowserFallbackStub)

    job = make_job()
    empty_payload = make_turbo_payload(job, json_payloads=[])
    push_calls = []

    async def mock_push(stream, message):
        push_calls.append(stream)

    with patch.object(service, "_perform_turbo_scrape", return_value=empty_payload), \
         patch.object(service.stream_queue, "push", side_effect=mock_push), \
         patch("worker_scraper.metrics_tracker") as mock_metrics:
        mock_metrics.record_job_status = AsyncMock()
        mock_metrics.increment = AsyncMock()
        await service.process_job(job)

    assert push_calls == ["raw_data_stream"], "exactly one push, from the browser fallback"


@pytest.mark.asyncio
async def test_endpoint_replay_never_targets_the_page_url():
    """Turbo endpoints promoted for a domain must never include the page URL itself."""
    service = make_service()
    domain = "api.example.com"
    page_url = "https://api.example.com/listing"

    job = make_job(url=page_url)
    job.overlay = None

    class _PromotingEngine:
        persona = None

        def __init__(self, *args, **kwargs):
            pass

        async def start(self, persona_id=None, proxy=None):
            return None

        async def crawl(self, url, network_idle=False, wait_selector=None):
            # Page returns HTML; its XHR call returns JSON from a distinct URL.
            return RawScrapePayload(
                job_id="x", target_site="universal", url=url, status_code=200,
                html_content="<html>page</html>",
                json_payloads=[{
                    "url": "https://api.example.com/v1/listing.json",
                    "status": 200,
                    "content_type": "application/json",
                    "data": {"ok": True},
                }],
            )

        async def close(self):
            return None

    with patch("worker_scraper.ScraperEngine", _PromotingEngine), \
         patch.object(service.stream_queue, "push", new_callable=AsyncMock), \
         patch("worker_scraper.metrics_tracker") as mock_metrics, \
         patch("worker_scraper.update_url_cache", new_callable=AsyncMock):
        mock_metrics.record_job_status = AsyncMock()
        mock_metrics.increment = AsyncMock()
        await service.process_job(job)

    promoted = service.domain_endpoints[domain]
    assert all(e["url"] != page_url for e in promoted)
    assert promoted == [{"url": "https://api.example.com/v1/listing.json", "content_type": "application/json"}]


def test_turbo_yield_failure_in_metric_keys():
    """turbo_yield_failure, turbo_endpoint_hit, turbo_endpoint_miss must be tracked metric keys."""
    from src.infrastructure.monitoring.observability import ObservabilityMetrics
    m = ObservabilityMetrics()
    assert "turbo_yield_failure" in m.metric_keys
    assert "turbo_endpoint_hit" in m.metric_keys
    assert "turbo_endpoint_miss" in m.metric_keys
