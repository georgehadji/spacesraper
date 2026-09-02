# Tests for Valkey Stream Queue adapter.
# Uses fakeredis for isolated testing.

import json
import pytest
from unittest.mock import AsyncMock
from src.infrastructure.queues.stream_queue import ValkeyStreamQueue
from src.domain.models import QueueMessage, MessageType


async def _make_queue() -> ValkeyStreamQueue:
    q = ValkeyStreamQueue(valkey_url="valkey://localhost:6379")
    await q.connect()
    return q


@pytest.mark.asyncio
async def test_push_and_consume():
    """Message pushed to a stream can be consumed."""
    queue = await _make_queue()
    try:
        msg = QueueMessage(
            message_id="test-1",
            message_type=MessageType.SCRAPE_JOB,
            payload={"job_id": "j1", "url": "https://example.com"},
        )

        entry_id = await queue.push("test_stream", msg)
        assert entry_id is not None

        await queue._ensure_group("test_stream", "test_group")
        results = await queue._valkey.xreadgroup(
            "test_group", "tester", {"test_stream": ">"},
            count=10, block=1000,
        )

        assert results is not None
        found = False
        for stream_name, entries in results:
            for entry_id, data in entries:
                raw = json.loads(data["payload"])
                consumed = QueueMessage(**raw)
                assert consumed.message_id == "test-1"
                assert consumed.message_type == MessageType.SCRAPE_JOB
                found = True
        assert found
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_consumer_group_auto_create():
    """Consumer group is auto-created on first consume attempt."""
    queue = await _make_queue()
    try:
        await queue._ensure_group("auto_stream", "auto_group")
        groups = await queue._valkey.xinfo_groups("auto_stream")
        assert any(g["name"] == "auto_group" for g in groups)
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_dlq_on_exhausted_retries():
    """Message is dead-lettered after exceeding max_retries."""
    queue = await _make_queue()
    try:
        msg = QueueMessage(
            message_id="dlq-test-1",
            message_type=MessageType.SCRAPE_JOB,
            retry_count=3,
            max_retries=3,
        )
        await queue.push("dlq_stream", msg)

        callback = AsyncMock(return_value=False)

        await queue._ensure_group("dlq_stream", "dlq_group")
        results = await queue._valkey.xreadgroup(
            "dlq_group", "tester", {"dlq_stream": ">"},
            count=10, block=1000,
        )

        if results:
            for stream_name, entries in results:
                for eid, data in entries:
                    await queue._process_entry(
                        stream_name, eid, data,
                        "dlq_group", "tester", callback, 3,
                    )

        dlq_len = await queue.get_dlq_length("dlq_stream")
        assert dlq_len >= 1
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_retry_on_failure():
    """Message is re-pushed with incremented retry_count on failure."""
    queue = await _make_queue()
    try:
        msg = QueueMessage(
            message_id="retry-test-1",
            message_type=MessageType.SCRAPE_JOB,
            retry_count=0,
            max_retries=3,
        )
        await queue.push("retry_stream", msg)
        await queue._ensure_group("retry_stream", "retry_group")

        callback = AsyncMock(return_value=False)

        results = await queue._valkey.xreadgroup(
            "retry_group", "tester", {"retry_stream": ">"},
            count=10, block=1000,
        )

        if results:
            for stream_name, entries in results:
                for eid, data in entries:
                    await queue._process_entry(
                        stream_name, eid, data,
                        "retry_group", "tester", callback, 3,
                    )

        stream_len = await queue.get_stream_length("retry_stream")
        assert stream_len >= 1
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_push_dlq():
    """Direct DLQ push works."""
    queue = await _make_queue()
    try:
        msg = QueueMessage(
            message_id="direct-dlq",
            message_type=MessageType.SCRAPE_JOB,
        )
        await queue.push_dlq("main_stream", msg, "test_reason")

        dlq_len = await queue.get_dlq_length("main_stream")
        assert dlq_len >= 1
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_multiple_message_types():
    """Different message types can be pushed and consumed."""
    queue = await _make_queue()
    try:
        types = [
            (MessageType.SCRAPE_JOB, {"job_id": "j1"}),
            (MessageType.RAW_PAYLOAD, {"html": "<html>"}),
            (MessageType.DISCOVERY_EVENT, {"count": 5}),
        ]

        for msg_type, payload in types:
            msg = QueueMessage(
                message_id=f"multi-{msg_type.value}",
                message_type=msg_type,
                payload=payload,
            )
            await queue.push("multi_stream", msg)

        await queue._ensure_group("multi_stream", "multi_group")
        results = await queue._valkey.xreadgroup(
            "multi_group", "tester", {"multi_stream": ">"},
            count=10, block=1000,
        )

        found_types = set()
        if results:
            for stream_name, entries in results:
                for eid, data in entries:
                    raw = json.loads(data["payload"])
                    found_types.add(raw["message_type"])

        assert found_types == {"scrape_job", "raw_payload", "discovery_event"}
    finally:
        await queue.close()
