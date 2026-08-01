import pytest
import json
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call
from src.domain.models import ScrapeJob
from src.infrastructure.queues.valkey_worker import ValkeyQueueWorker


def make_job(job_id="job-oom-1"):
    return ScrapeJob(job_id=job_id, url="https://example.com", target_site="test")


@pytest.mark.asyncio
async def test_oom_drop_pushes_to_dlq():
    """When OOM hard limit is hit, job must be routed to DLQ via push_dead_letter."""
    worker = ValkeyQueueWorker()
    worker._is_mock = False

    job = make_job()
    dlq_calls = []

    async def mock_info(section=None):
        return {"used_memory_rss": 800 * 1024 * 1024}  # 800MB

    async def mock_push_dead_letter(queue_name, item, reason):
        dlq_calls.append((queue_name, reason))

    worker.valkey = MagicMock()
    worker.valkey.info = mock_info
    worker.valkey.incrby = AsyncMock()
    worker.push_dead_letter = mock_push_dead_letter

    await worker.push_job("jobs_queue", job)

    assert len(dlq_calls) == 1
    assert dlq_calls[0][0] == "jobs_queue"
    assert dlq_calls[0][1] == "OOM_BACKPRESSURE"


@pytest.mark.asyncio
async def test_oom_drop_increments_metric():
    """When OOM hard limit is hit, jobs_dropped_oom metric must be incremented."""
    worker = ValkeyQueueWorker()
    worker._is_mock = False

    job = make_job()
    incr_calls = []

    async def mock_info(section=None):
        return {"used_memory_rss": 800 * 1024 * 1024}

    async def mock_incrby(key, amount):
        incr_calls.append((key, amount))

    worker.valkey = MagicMock()
    worker.valkey.info = mock_info
    worker.valkey.incrby = mock_incrby
    worker.push_dead_letter = AsyncMock()

    await worker.push_job("jobs_queue", job)

    assert any("dropped_oom" in key for key, _ in incr_calls), \
        f"Expected jobs_dropped_oom increment but got: {incr_calls}"


@pytest.mark.asyncio
async def test_normal_job_not_affected_below_threshold():
    """Jobs below the soft memory limit must be enqueued normally."""
    worker = ValkeyQueueWorker()
    worker._is_mock = False

    job = make_job()
    pushed = []

    async def mock_info(section=None):
        return {"used_memory_rss": 100 * 1024 * 1024}  # 100MB — well below limit

    async def mock_rpush(queue_name, payload):
        pushed.append(queue_name)

    worker.valkey = MagicMock()
    worker.valkey.info = mock_info
    worker.valkey.rpush = mock_rpush

    await worker.push_job("jobs_queue", job)

    assert "jobs_queue" in pushed
    assert not any("dlq" in q for q in pushed)
