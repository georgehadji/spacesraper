# P4 queue-delivery guards: D4 (discovery publishes where nobody reads) and
# D13 (a claimed pending message can never make progress).
#
# Both defects share one property: a message that enters the system must be
# able to leave it. D4 breaks that at the producer (wrong transport entirely),
# D13 at the redelivery path (claimed, unparseable, re-claimed forever).

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.application.discovery_service import DiscoveryResult
from src.domain.models import JobState, MessageType, QueueMessage, ResearchPlan, ScrapeJob
from src.infrastructure.queues.stream_queue import DLQ_SUFFIX, ValkeyStreamQueue

REPO_ROOT = Path(__file__).resolve().parents[1]

# The one stream the scraper consumes. Producer and consumer name it with
# separate string literals, which is exactly how D4 happened.
SCRAPER_STREAM = "jobs_stream"
SCRAPER_GROUP = "scrapers"


async def _fake_queue() -> ValkeyStreamQueue:
    """A queue backed by fakeredis (connect() falls back when Valkey is absent)."""
    queue = ValkeyStreamQueue(valkey_url="valkey://localhost:6379")
    await queue.connect()
    return queue


def _make_message(plan_id: str) -> QueueMessage:
    return QueueMessage(
        message_id="m-1",
        message_type=MessageType.DISCOVERY_QUERY,
        payload={"plan_id": plan_id},
    )


# --- D4: discovery enqueues to a queue nobody reads ---


@pytest.mark.asyncio
async def test_a_discovered_job_is_readable_by_the_scrapers_consumer_group():
    """The property: an enqueued job is eventually consumable.

    Discovery used to RPUSH onto a Valkey *list* called "jobs_queue" while the
    scraper XREADGROUPs a *stream* called "jobs_stream". Nothing read the list,
    so every discovered job vanished. Asserting on the producer's call alone
    would not have caught that — this reads the job back the way the scraper
    does.
    """
    from worker_discovery import DiscoveryWorkerService

    worker = DiscoveryWorkerService()
    queue = await _fake_queue()
    worker.stream_queue = queue

    plan = ResearchPlan(plan_id="rp-d4", query="q", allowed_domains=["example.com"])
    worker.plan_repo = AsyncMock()
    worker.plan_repo.get_plan = AsyncMock(return_value=plan)
    worker.artifact_store = AsyncMock()
    worker.artifact_store.store = AsyncMock(return_value="sha")

    job = ScrapeJob(job_id="disc_1", url="https://example.com/a", target_site="example.com")
    worker.discovery_service = AsyncMock()
    worker.discovery_service.discover = AsyncMock(return_value=DiscoveryResult([job], {}, []))

    try:
        with patch("worker_discovery.settings") as mock_settings, \
             patch("worker_discovery.metrics_tracker") as mock_metrics:
            mock_settings.features = {"discovery": True}
            mock_metrics.increment = AsyncMock()
            assert await worker.handle_discovery_query(_make_message("rp-d4")) is True

        await queue._ensure_group(SCRAPER_STREAM, SCRAPER_GROUP)
        results = await queue._valkey.xreadgroup(
            SCRAPER_GROUP, "scraper-test", {SCRAPER_STREAM: ">"}, count=10, block=100,
        )

        delivered = [
            QueueMessage(**json.loads(data["payload"]))
            for _stream, entries in (results or [])
            for _entry_id, data in entries
        ]
        assert len(delivered) == 1, (
            f"scraper's consumer group saw {len(delivered)} jobs on {SCRAPER_STREAM}"
        )
        assert delivered[0].message_type == MessageType.SCRAPE_JOB
        assert delivered[0].payload["url"] == "https://example.com/a"
    finally:
        await queue.close()


def test_discovery_and_the_scraper_name_the_same_stream():
    """Producer and consumer agree on the transport, in source.

    D4's whole mechanism was two literals drifting apart. This fails if either
    side is renamed without the other.
    """
    scraper = (REPO_ROOT / "worker_scraper.py").read_text(encoding="utf-8")
    discovery = (REPO_ROOT / "worker_discovery.py").read_text(encoding="utf-8")

    assert f'"{SCRAPER_STREAM}", "{SCRAPER_GROUP}"' in scraper, (
        "worker_scraper.py no longer consumes the stream this test tracks"
    )
    assert SCRAPER_STREAM in discovery, "worker_discovery.py does not publish to the scraper's stream"
    # Checked against code, not prose — the comment explaining D4 legitimately
    # names the old queue.
    assert "push_job(" not in discovery, (
        "worker_discovery.py still pushes through the list-queue API nothing consumes"
    )
    assert "RedisQueueWorker" not in discovery, (
        "worker_discovery.py still constructs the deleted third queue implementation"
    )


# --- D13: a claimed pending message can never make progress ---


@pytest.mark.asyncio
async def test_a_claimed_pending_message_reaches_the_callback():
    """XCLAIM's reply shape differs from XREADGROUP's.

    _claim_pending re-wrapped the claimed fields as {stream: data}, so
    _process_entry's data.get("payload") missed and produced QueueMessage(**{}),
    a ValidationError outside the caught tuple. The orphan was re-claimed every
    60s forever and the callback never ran.
    """
    queue = await _fake_queue()
    try:
        msg = QueueMessage(
            message_id="claim-1",
            message_type=MessageType.SCRAPE_JOB,
            payload={"job_id": "j1", "url": "https://example.com"},
        )
        await queue.push("claim_stream", msg)
        await queue._ensure_group("claim_stream", "claim_group")

        # Deliver to a consumer that then "dies" without acking, leaving the
        # entry pending.
        await queue._valkey.xreadgroup(
            "claim_group", "dead-consumer", {"claim_stream": ">"}, count=10, block=100,
        )

        seen: list[QueueMessage] = []

        async def callback(message: QueueMessage) -> bool:
            seen.append(message)
            return True

        await queue._claim_pending(
            "claim_stream", "claim_group", "live-consumer",
            callback, max_retries=3, claim_idle_ms=0,
        )

        assert [m.message_id for m in seen] == ["claim-1"]
        assert await queue.get_pending_count("claim_stream", "claim_group") == 0, (
            "claimed message is still pending — it will be re-claimed forever"
        )
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_an_unparseable_entry_is_dead_lettered_rather_than_dropped():
    """An entry that cannot become a QueueMessage must leave the pending set
    *and* be recoverable. Acking it silently loses the payload; raising spins."""
    queue = await _fake_queue()
    try:
        await queue._ensure_group("bad_stream", "bad_group")
        # Valid JSON, but not a QueueMessage — pydantic raises ValidationError,
        # which is not a JSONDecodeError/KeyError/TypeError.
        entry_id = await queue._valkey.xadd("bad_stream", {"payload": json.dumps({"nonsense": 1})})
        await queue._valkey.xreadgroup(
            "bad_group", "tester", {"bad_stream": ">"}, count=10, block=100,
        )

        callback = AsyncMock(return_value=True)
        await queue._process_entry(
            "bad_stream", entry_id, {"payload": json.dumps({"nonsense": 1})},
            "bad_group", "tester", callback, 3,
        )

        callback.assert_not_called()
        assert await queue.get_pending_count("bad_stream", "bad_group") == 0
        assert await queue.get_stream_length("bad_stream" + DLQ_SUFFIX) >= 1, (
            "unparseable entry was discarded instead of dead-lettered"
        )
    finally:
        await queue.close()


# --- behaviour-preservation controls ---


@pytest.mark.asyncio
async def test_discovery_still_acks_and_records_child_ids():
    """The D4 transport change must not alter the plan bookkeeping."""
    from worker_discovery import DiscoveryWorkerService

    worker = DiscoveryWorkerService()
    queue = await _fake_queue()
    worker.stream_queue = queue

    plan = ResearchPlan(plan_id="rp-ctl", query="q", allowed_domains=["example.com"])
    worker.plan_repo = AsyncMock()
    worker.plan_repo.get_plan = AsyncMock(return_value=plan)
    worker.artifact_store = AsyncMock()
    worker.artifact_store.store = AsyncMock(return_value="sha")

    job = ScrapeJob(job_id="disc_ctl", url="https://example.com/b", target_site="example.com")
    worker.discovery_service = AsyncMock()
    worker.discovery_service.discover = AsyncMock(return_value=DiscoveryResult([job], {}, []))

    try:
        with patch("worker_discovery.settings") as mock_settings, \
             patch("worker_discovery.metrics_tracker") as mock_metrics:
            mock_settings.features = {"discovery": True}
            mock_metrics.increment = AsyncMock()
            assert await worker.handle_discovery_query(_make_message("rp-ctl")) is True

        worker.plan_repo.set_child_job_ids.assert_called_once_with("rp-ctl", ["disc_ctl"])
        states = [c.args[1] for c in worker.plan_repo.update_plan_state.call_args_list]
        assert states == [JobState.RUNNING, JobState.SUCCEEDED]
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_a_well_formed_entry_still_acks_on_success():
    """Control for the widened except: the happy path is unchanged."""
    queue = await _fake_queue()
    try:
        msg = QueueMessage(message_id="ok-1", message_type=MessageType.SCRAPE_JOB, payload={})
        await queue.push("ok_stream", msg)
        await queue._ensure_group("ok_stream", "ok_group")
        results = await queue._valkey.xreadgroup(
            "ok_group", "tester", {"ok_stream": ">"}, count=10, block=100,
        )
        callback = AsyncMock(return_value=True)
        for _stream, entries in results:
            for entry_id, data in entries:
                await queue._process_entry(
                    "ok_stream", entry_id, data, "ok_group", "tester", callback, 3,
                )

        callback.assert_awaited_once()
        assert await queue.get_pending_count("ok_stream", "ok_group") == 0
        assert await queue.get_stream_length("ok_stream" + DLQ_SUFFIX) == 0
    finally:
        await queue.close()
