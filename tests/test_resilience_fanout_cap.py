import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.domain.models import RawScrapePayload, ProcessingResult, FollowLink, Opportunity
from worker_processor import ProcessorWorkerService
from src.infrastructure.queues.valkey_worker import ValkeyQueueWorker


def make_payload(job_id="root-job-1", depth=0):
    return RawScrapePayload(
        job_id=job_id,
        target_site="test_source",
        url="https://example.com/listing",
        status_code=200,
        html_content="<html></html>"
    )


def make_follow_links(count, base_url="https://example.com/opportunity/"):
    return [{"url": f"{base_url}{i}", "target_site": "test_source", "depth": 1}
            for i in range(count)]


@pytest.mark.asyncio
async def test_follow_urls_within_cap_all_enqueued():
    """Follow URLs within the MAX_RECURSIVE_FANOUT limit must all be queued."""
    service = ProcessorWorkerService()
    payload = make_payload()

    result = ProcessingResult(
        job_id=payload.job_id,
        success=True,
        entities=[],
        follow_urls=make_follow_links(10)
    )

    enqueued = []

    async def mock_push_job(queue_name, job):
        enqueued.append(job.url)

    async def mock_fanout_check(root_id, count, max_fanout):
        return count  # All allowed

    with patch.object(service.pipeline, "process", return_value=result), \
         patch.object(service.post_processor, "run_state_audit",
                      return_value=({"NEW": 0, "UPDATED": 0, "UNCHANGED": 0}, [])), \
         patch.object(service.queue, "push_job", side_effect=mock_push_job), \
         patch.object(service.queue, "get_allowed_fanout",
                      side_effect=mock_fanout_check), \
         patch("worker_processor.metrics_tracker") as mock_metrics:
        mock_metrics.increment = AsyncMock()
        mock_metrics.record_job_status = AsyncMock()
        await service.process_payload(payload)

    assert len(enqueued) == 10


@pytest.mark.asyncio
async def test_follow_urls_over_cap_are_limited():
    """Follow URLs exceeding MAX_RECURSIVE_FANOUT must be capped; excess dropped to DLQ."""
    service = ProcessorWorkerService()
    payload = make_payload()

    result = ProcessingResult(
        job_id=payload.job_id,
        success=True,
        entities=[],
        follow_urls=make_follow_links(250)  # Exceeds cap of 200
    )

    enqueued = []
    dlq_pushes = []

    async def mock_push_job(queue_name, job):
        enqueued.append(job.url)

    async def mock_push_dlq(queue_name, job, reason):
        dlq_pushes.append((job.url, reason))

    async def mock_fanout_check(root_id, count, max_fanout):
        return min(count, max_fanout)  # Cap at max

    with patch.object(service.pipeline, "process", return_value=result), \
         patch.object(service.post_processor, "run_state_audit",
                      return_value=({"NEW": 0, "UPDATED": 0, "UNCHANGED": 0}, [])), \
         patch.object(service.queue, "push_job", side_effect=mock_push_job), \
         patch.object(service.queue, "push_dead_letter", side_effect=mock_push_dlq), \
         patch.object(service.queue, "get_allowed_fanout",
                      side_effect=mock_fanout_check), \
         patch("worker_processor.metrics_tracker") as mock_metrics:
        mock_metrics.increment = AsyncMock()
        mock_metrics.record_job_status = AsyncMock()
        await service.process_payload(payload)

    assert len(enqueued) == service.MAX_RECURSIVE_FANOUT
    assert len(dlq_pushes) == 250 - service.MAX_RECURSIVE_FANOUT
    mock_metrics.increment.assert_any_call("fanout_cap_drops", 50)


@pytest.mark.asyncio
async def test_root_id_extracted_correctly_at_depth_2():
    """
    A depth-2 recursive job (rec_rec_root) must resolve to the same root budget
    key as a depth-1 job (rec_root) — not a separate per-depth key.
    """
    service = ProcessorWorkerService()
    # Simulate a depth-2 payload
    payload = make_payload(job_id="rec_rec_root-job-1")
    result = ProcessingResult(
        job_id=payload.job_id,
        success=True,
        entities=[],
        follow_urls=make_follow_links(5)
    )

    captured_root_ids = []

    async def mock_fanout_check(root_id, count, max_fanout):
        captured_root_ids.append(root_id)
        return count

    with patch.object(service.pipeline, "process", return_value=result), \
         patch.object(service.post_processor, "run_state_audit",
                      return_value=({"NEW": 0, "UPDATED": 0, "UNCHANGED": 0}, [])), \
         patch.object(service.queue, "push_job", new_callable=AsyncMock), \
         patch.object(service.queue, "get_allowed_fanout",
                      side_effect=mock_fanout_check), \
         patch("worker_processor.metrics_tracker") as mock_metrics:
        mock_metrics.increment = AsyncMock()
        mock_metrics.record_job_status = AsyncMock()
        await service.process_payload(payload)

    assert len(captured_root_ids) == 1
    assert captured_root_ids[0] == "root-job-1", \
        f"Expected 'root-job-1' but got '{captured_root_ids[0]}'"


@pytest.mark.asyncio
async def test_get_allowed_fanout_atomic_with_fakeredis():
    """
    get_allowed_fanout must enforce the budget correctly against a real Valkey.
    Uses fakeredis for an in-process Redis simulation.
    """
    try:
        import fakeredis
    except ImportError:
        pytest.skip("fakeredis not installed")

    fake_valkey = fakeredis.FakeAsyncValkey(decode_responses=True)
    worker = ValkeyQueueWorker()
    worker.valkey = fake_valkey
    worker._is_mock = False  # Force real path

    # First call: 150 requested, max 200 — all 150 should be allowed
    allowed1 = await worker.get_allowed_fanout("root-abc", 150, 200)
    assert allowed1 == 150, f"Expected 150, got {allowed1}"

    # Second call: 100 requested, only 50 remaining (200 - 150)
    allowed2 = await worker.get_allowed_fanout("root-abc", 100, 200)
    assert allowed2 == 50, f"Expected 50, got {allowed2}"

    # Third call: budget exhausted — should return 0
    allowed3 = await worker.get_allowed_fanout("root-abc", 10, 200)
    assert allowed3 == 0, f"Expected 0, got {allowed3}"

    await fake_valkey.aclose()
