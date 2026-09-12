# P4 worker guards: D18 (reporter acks on total delivery failure, and blocks
# the event loop on pandas) and D19 (URL marked seen before its children are
# enqueued).
#
# Shared property: a worker must not report progress it did not make. D18
# reports a delivery that never happened; D19 records a fan-out that never
# happened, which is worse — the redelivery that would have fixed it computes
# an empty follow set and the children are lost for good.

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.models import MessageType, QueueMessage, RawScrapePayload

# --- D18: reporter acks on total delivery failure ---


def _reporter(plugins):
    from worker_reporter import ReporterWorkerService

    worker = ReporterWorkerService(stream_queue=MagicMock())
    worker.report_gen = MagicMock()
    worker.plugins = plugins
    return worker


def _event_message() -> QueueMessage:
    return QueueMessage(
        message_id="ev-1",
        message_type=MessageType.DISCOVERY_EVENT,
        root_job_id="job-1",
        payload={
            "job_id": "job-1",
            "target_site": "example.com",
            "new_count": 1,
            "updated_count": 0,
            "entities": [],
        },
    )


def _plugin(*, fails: bool):
    plugin = MagicMock()
    if fails:
        plugin.deliver = AsyncMock(side_effect=RuntimeError("webhook 500"))
    else:
        plugin.deliver = AsyncMock(return_value=None)
    return plugin


@pytest.mark.asyncio
async def test_a_run_where_every_delivery_failed_is_not_acked():
    """gather(return_exceptions=True) whose results are discarded turns a total
    delivery failure into a success ack — the message is gone and nothing was
    delivered."""
    worker = _reporter([_plugin(fails=True), _plugin(fails=True)])

    assert await worker.process_stream_message(_event_message()) is False, (
        "reporter acked a run in which every delivery raised"
    )


@pytest.mark.asyncio
async def test_a_partial_delivery_failure_still_acks():
    """Retrying the whole message would re-deliver to the channels that already
    succeeded. Partial failure is logged, not nacked."""
    worker = _reporter([_plugin(fails=True), _plugin(fails=False)])

    assert await worker.process_stream_message(_event_message()) is True


@pytest.mark.asyncio
async def test_the_report_generator_does_not_run_on_the_event_loop():
    """generate_excel_csv is synchronous pandas + openpyxl I/O. Called directly
    it stalls every other coroutine in the reporter for the length of an Excel
    write."""
    worker = _reporter([])
    calling_threads: list[str] = []
    worker.report_gen.generate_excel_csv = MagicMock(
        side_effect=lambda *a, **k: calling_threads.append(threading.current_thread().name)
    )

    assert await worker.process_stream_message(_event_message()) is True

    assert calling_threads, "report generator was never called"
    assert calling_threads[0] != threading.current_thread().name, (
        f"generate_excel_csv ran on the event loop thread ({calling_threads[0]})"
    )


@pytest.mark.asyncio
async def test_a_run_with_no_plugins_configured_still_acks():
    """Control: no delivery channels is not a delivery failure."""
    worker = _reporter([])

    assert await worker.process_stream_message(_event_message()) is True


# --- D19: URL marked seen before its children are enqueued ---


def _processor():
    from worker_processor import ProcessorWorkerService

    worker = ProcessorWorkerService(
        stream_queue=AsyncMock(),
        job_repo=AsyncMock(),
        record_repo=AsyncMock(),
        overlay_repo=AsyncMock(),
        intel_tracker=AsyncMock(),
    )
    worker.post_processor = AsyncMock()
    worker.post_processor.run_state_audit = AsyncMock(
        return_value=({"NEW": 0, "UPDATED": 0, "UNCHANGED": 0}, [])
    )
    worker.pipeline = AsyncMock()
    worker.stream_queue.get_allowed_fanout = AsyncMock(return_value=2)
    return worker


def _payload() -> RawScrapePayload:
    return RawScrapePayload(
        job_id="root1", target_site="example.com",
        url="https://example.com/", status_code=200,
    )


def _follows():
    return [
        {"url": "https://example.com/a", "target_site": "example.com"},
        {"url": "https://example.com/b", "target_site": "example.com"},
    ]


def _result(follow_urls):
    return SimpleNamespace(success=True, entities=[], follow_urls=follow_urls, error=None)


@pytest.mark.asyncio
async def test_urls_are_not_marked_seen_when_their_enqueue_fails():
    """seen.update ran before the fan-out loop, so a raise mid-enqueue left the
    URLs recorded as discovered. The stream redelivers the parent, the dedup
    filter removes them as already-seen, and nobody ever enqueues them."""
    worker = _processor()
    worker.pipeline.process = AsyncMock(return_value=_result(_follows()))
    worker.stream_queue.push = AsyncMock(side_effect=RuntimeError("valkey down"))

    with pytest.raises(RuntimeError):
        await worker.process_payload(_payload())

    seen = worker._seen_urls.get("root1", set())
    assert "https://example.com/a" not in seen, (
        "URL was marked seen although its child job was never enqueued"
    )
    assert "https://example.com/b" not in seen


@pytest.mark.asyncio
async def test_a_partially_enqueued_fanout_keeps_only_what_was_enqueued():
    """The second push fails. The first child is enqueued and must stay seen;
    the second must not, so a redelivery can still enqueue it."""
    worker = _processor()
    worker.pipeline.process = AsyncMock(return_value=_result(_follows()))
    calls: list[str] = []

    async def push(stream, message, **kwargs):
        url = message.payload.get("url", "")
        calls.append(url)
        if len(calls) == 2:
            raise RuntimeError("valkey down")

    worker.stream_queue.push = AsyncMock(side_effect=push)

    with pytest.raises(RuntimeError):
        await worker.process_payload(_payload())

    seen = worker._seen_urls.get("root1", set())
    assert calls[0] in seen, "enqueued child was not recorded as seen"
    assert calls[1] not in seen, "child whose enqueue raised was recorded as seen"


@pytest.mark.asyncio
async def test_a_successful_fanout_still_dedups_on_redelivery():
    """Control: the ordering change must not weaken dedup. A second run over
    the same follows enqueues nothing."""
    worker = _processor()
    worker.pipeline.process = AsyncMock(return_value=_result(_follows()))
    worker.stream_queue.push = AsyncMock(return_value="1-1")

    await worker.process_payload(_payload())
    assert worker.stream_queue.push.await_count == 2
    assert worker._seen_urls["root1"] == {
        "https://example.com/a", "https://example.com/b",
    }

    worker.stream_queue.push.reset_mock()
    worker.pipeline.process = AsyncMock(return_value=_result(_follows()))
    await worker.process_payload(_payload())
    assert worker.stream_queue.push.await_count == 0, "dedup no longer suppresses a revisit"


@pytest.mark.asyncio
async def test_fanout_budget_is_still_consulted_once_per_run():
    """Control: the budget check stays outside the enqueue loop — moving it in
    would spend one EVAL per child."""
    worker = _processor()
    worker.pipeline.process = AsyncMock(return_value=_result(_follows()))
    worker.stream_queue.push = AsyncMock(return_value="1-1")

    await worker.process_payload(_payload())

    worker.stream_queue.get_allowed_fanout.assert_awaited_once()
    assert asyncio.get_running_loop() is not None  # sanity: ran on a loop
