"""
Task 3.4 — Tests for DiscoveryWorkerService (worker_discovery.py).
Verifies the research_stream consumer wires DiscoveryService correctly and
that plan state, child job IDs, and the SERP archive all end up consistent.
"""

import os
import pytest
from unittest.mock import AsyncMock, patch

from worker_discovery import DiscoveryWorkerService
from src.domain.models import QueueMessage, MessageType, ResearchPlan, JobState, SearchHit
from src.infrastructure.repositories.research_plan_repository import SqliteResearchPlanRepository


def make_message(plan_id="rp-worker-1", **payload_overrides):
    payload = {
        "plan_id": plan_id,
        "query": "test query",
        "max_results": 10,
        "allowed_domains": ["example.com"],
    }
    payload.update(payload_overrides)
    return QueueMessage(
        message_id="msg-1",
        message_type=MessageType.DISCOVERY_QUERY,
        root_job_id=plan_id,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_discovery_disabled_acks_without_running():
    worker = DiscoveryWorkerService()
    worker.plan_repo = AsyncMock()

    with patch("worker_discovery.settings") as mock_settings:
        mock_settings.features = {"discovery": False}
        result = await worker.handle_discovery_query(make_message())

    assert result is True
    worker.plan_repo.get_plan.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_plan_acks_and_drops():
    worker = DiscoveryWorkerService()
    worker.plan_repo = AsyncMock()
    worker.plan_repo.get_plan = AsyncMock(return_value=None)

    with patch("worker_discovery.settings") as mock_settings:
        mock_settings.features = {"discovery": True}
        result = await worker.handle_discovery_query(make_message(plan_id="nonexistent"))

    assert result is True


@pytest.mark.asyncio
async def test_successful_discovery_updates_plan_and_enqueues_jobs():
    worker = DiscoveryWorkerService()

    plan = ResearchPlan(
        plan_id="rp-success", query="test query", allowed_domains=["example.com"]
    )
    worker.plan_repo = AsyncMock()
    worker.plan_repo.get_plan = AsyncMock(return_value=plan)
    worker.plan_repo.update_plan_state = AsyncMock()
    worker.plan_repo.set_child_job_ids = AsyncMock()
    worker.plan_repo.set_serp_artifact_sha = AsyncMock()

    hits = [SearchHit(url="https://example.com/a", title="A", rank=0, provider="test")]
    worker.search_provider = AsyncMock()
    worker.search_provider.search = AsyncMock(return_value=hits)

    fake_job = AsyncMock()
    fake_job.job_id = "disc_abc123"
    fake_job.url = "https://example.com/a"

    worker.discovery_service = AsyncMock()
    worker.discovery_service.discover = AsyncMock(return_value=([fake_job], {}))

    worker.queue = AsyncMock()
    worker.queue.push_job = AsyncMock()

    worker.artifact_store = AsyncMock()
    worker.artifact_store.store = AsyncMock(return_value="fakesha256")

    with patch("worker_discovery.settings") as mock_settings, \
         patch("worker_discovery.metrics_tracker") as mock_metrics:
        mock_settings.features = {"discovery": True}
        mock_metrics.increment = AsyncMock()
        result = await worker.handle_discovery_query(make_message(plan_id="rp-success"))

    assert result is True
    worker.queue.push_job.assert_called_once_with("jobs_queue", fake_job)
    worker.plan_repo.set_child_job_ids.assert_called_once_with("rp-success", ["disc_abc123"])
    worker.plan_repo.set_serp_artifact_sha.assert_called_once_with("rp-success", "fakesha256")
    # RUNNING then SUCCEEDED
    states = [call.args[1] for call in worker.plan_repo.update_plan_state.call_args_list]
    assert states == [JobState.RUNNING, JobState.SUCCEEDED]


@pytest.mark.asyncio
async def test_discovery_refused_marks_plan_failed_but_acks():
    from src.domain.exceptions import DiscoveryRefusedError

    worker = DiscoveryWorkerService()
    plan = ResearchPlan(plan_id="rp-refused", query="q", allowed_domains=[])
    worker.plan_repo = AsyncMock()
    worker.plan_repo.get_plan = AsyncMock(return_value=plan)
    worker.plan_repo.update_plan_state = AsyncMock()

    worker.discovery_service = AsyncMock()
    worker.discovery_service.discover = AsyncMock(
        side_effect=DiscoveryRefusedError("empty allowlist")
    )

    with patch("worker_discovery.settings") as mock_settings:
        mock_settings.features = {"discovery": True}
        result = await worker.handle_discovery_query(make_message(plan_id="rp-refused"))

    assert result is True  # refusal is a policy outcome, not a transient failure
    worker.plan_repo.update_plan_state.assert_any_call(
        "rp-refused", JobState.FAILED, error_message="empty allowlist"
    )


@pytest.mark.asyncio
async def test_unexpected_error_marks_failed_and_signals_retry():
    worker = DiscoveryWorkerService()
    plan = ResearchPlan(plan_id="rp-error", query="q", allowed_domains=["example.com"])
    worker.plan_repo = AsyncMock()
    worker.plan_repo.get_plan = AsyncMock(return_value=plan)
    worker.plan_repo.update_plan_state = AsyncMock()

    worker.discovery_service = AsyncMock()
    worker.discovery_service.discover = AsyncMock(side_effect=RuntimeError("boom"))

    with patch("worker_discovery.settings") as mock_settings:
        mock_settings.features = {"discovery": True}
        result = await worker.handle_discovery_query(make_message(plan_id="rp-error"))

    assert result is False  # transient — let the stream consumer retry/DLQ
    worker.plan_repo.update_plan_state.assert_any_call(
        "rp-error", JobState.FAILED, error_message="boom"
    )
