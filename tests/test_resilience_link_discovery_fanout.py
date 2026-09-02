# P2 resilience: recursion respects max_depth, and dedups revisits within a
# crawl tree instead of re-enqueuing the same URL twice.
# docs/plans/2026-08-13-capability-enhancement-plan.md P2 deliverable.

from unittest.mock import AsyncMock, patch

import pytest

from src.application.extraction_pipeline import ExtractionPipeline
from src.domain.models import ProcessingResult, RawScrapePayload
from src.extractors.strategies import GenericStrategy
from worker_processor import ProcessorWorkerService


def make_payload(job_id="root-job-1", **overrides):
    fields = dict(
        job_id=job_id, target_site="test_source", url="https://example.com/listing",
        status_code=200, html_content="<html></html>",
    )
    fields.update(overrides)
    return RawScrapePayload(**fields)


def make_follow_links(urls, depth=1):
    return [{"url": u, "target_site": "test_source", "depth": depth} for u in urls]


@pytest.mark.asyncio
async def test_depth_at_budget_stops_recursion():
    """A page at depth == max_depth must not discover further links — the
    crawl terminates instead of overshooting its budget."""
    html = '<a href="https://example.com/next">Next</a>'
    payload = RawScrapePayload(
        job_id="j1", target_site="universal", url="https://example.com/",
        status_code=200, html_content=html, depth=3, max_depth=3, follow_links=True,
    )
    pipeline = ExtractionPipeline()
    result = await pipeline.process(payload, GenericStrategy())
    assert result.follow_urls == []


@pytest.mark.asyncio
async def test_depth_below_budget_discovers_links():
    html = '<a href="https://example.com/next">Next</a>'
    payload = RawScrapePayload(
        job_id="j1", target_site="universal", url="https://example.com/",
        status_code=200, html_content=html, depth=1, max_depth=3, follow_links=True,
    )
    pipeline = ExtractionPipeline()
    result = await pipeline.process(payload, GenericStrategy())
    assert [f["url"] for f in result.follow_urls] == ["https://example.com/next"]
    assert result.follow_urls[0]["depth"] == 2


@pytest.mark.asyncio
async def test_follow_links_disabled_by_default_discovers_nothing():
    """The opt-in flag must actually gate discovery — existing single-page
    callers that never set follow_links see zero behavior change."""
    html = '<a href="https://example.com/next">Next</a>'
    payload = make_payload(html_content=html)  # follow_links defaults False
    pipeline = ExtractionPipeline()
    result = await pipeline.process(payload, GenericStrategy())
    assert result.follow_urls == []


@pytest.mark.asyncio
async def test_revisit_within_same_crawl_tree_is_not_re_enqueued():
    """Two pages in the same crawl (same root_id) both discover the same
    URL — it must be enqueued once, not twice."""
    service = ProcessorWorkerService()
    shared_url = "https://example.com/dup"

    enqueued = []

    async def mock_push(stream, message):
        enqueued.append(message.payload["url"])
        return "1-0"

    async def mock_fanout_check(root_id, count, max_fanout):
        return count

    with patch.object(service.post_processor, "run_state_audit",
                       return_value=({"NEW": 0, "UPDATED": 0, "UNCHANGED": 0}, [])), \
         patch.object(service.stream_queue, "push", side_effect=mock_push), \
         patch.object(service.stream_queue, "get_allowed_fanout", side_effect=mock_fanout_check), \
         patch("worker_processor.metrics_tracker") as mock_metrics:
        mock_metrics.increment = AsyncMock()
        mock_metrics.record_job_status = AsyncMock()

        first_result = ProcessingResult(
            job_id="root-job-1", success=True, entities=[],
            follow_urls=make_follow_links([shared_url, "https://example.com/other"]),
        )
        with patch.object(service.pipeline, "process", return_value=first_result):
            await service.process_payload(make_payload(job_id="root-job-1"))

        second_result = ProcessingResult(
            job_id="rec_root-job-1", success=True, entities=[],
            follow_urls=make_follow_links([shared_url]),  # same URL, different page
        )
        with patch.object(service.pipeline, "process", return_value=second_result):
            await service.process_payload(make_payload(job_id="rec_root-job-1"))

    assert enqueued == ["https://example.com/dup", "https://example.com/other"]
