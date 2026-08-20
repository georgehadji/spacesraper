from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.models import MessageType, QueueMessage
from src.infrastructure.queues.stream_queue import ValkeyStreamQueue, make_message


def make_msg(message_id="job-oom-1"):
    return make_message(
        MessageType.SCRAPE_JOB,
        {"job_id": message_id, "url": "https://example.com"},
    )


@pytest.mark.asyncio
async def test_oom_drop_pushes_to_dlq():
    """When OOM hard limit is hit, message must be routed to DLQ via push_dlq."""
    queue = ValkeyStreamQueue()
    queue._is_mock = False

    msg = make_msg()
    dlq_calls = []

    async def mock_info(section=None):
        return {"used_memory_rss": 800 * 1024 * 1024}  # 800MB

    async def mock_push_dlq(stream, message, reason):
        dlq_calls.append((stream, reason))

    queue._valkey = MagicMock()
    queue._valkey.info = mock_info
    queue._valkey.incrby = AsyncMock()
    queue.push_dlq = mock_push_dlq

    entry_id = await queue.push("jobs_stream", msg)

    assert entry_id == ""
    assert len(dlq_calls) == 1
    assert dlq_calls[0][0] == "jobs_stream"
    assert dlq_calls[0][1] == "OOM_BACKPRESSURE"


@pytest.mark.asyncio
async def test_oom_drop_increments_metric():
    """When OOM hard limit is hit, jobs_dropped_oom metric must be incremented."""
    queue = ValkeyStreamQueue()
    queue._is_mock = False

    msg = make_msg()
    incr_calls = []

    async def mock_info(section=None):
        return {"used_memory_rss": 800 * 1024 * 1024}

    async def mock_incrby(key, amount):
        incr_calls.append((key, amount))

    queue._valkey = MagicMock()
    queue._valkey.info = mock_info
    queue._valkey.incrby = mock_incrby
    queue.push_dlq = AsyncMock()

    await queue.push("jobs_stream", msg)

    assert any("dropped_oom" in key for key, _ in incr_calls), \
        f"Expected jobs_dropped_oom increment but got: {incr_calls}"


@pytest.mark.asyncio
async def test_normal_message_not_affected_below_threshold():
    """Messages below the soft memory limit must be enqueued normally."""
    queue = ValkeyStreamQueue()
    queue._is_mock = False

    msg = make_msg()
    pushed = []

    async def mock_info(section=None):
        return {"used_memory_rss": 100 * 1024 * 1024}  # 100MB — well below limit

    async def mock_xadd(stream, fields, maxlen=None):
        pushed.append(stream)
        return "1-0"

    queue._valkey = MagicMock()
    queue._valkey.info = mock_info
    queue._valkey.xadd = mock_xadd

    entry_id = await queue.push("jobs_stream", msg)

    assert entry_id == "1-0"
    assert "jobs_stream" in pushed
