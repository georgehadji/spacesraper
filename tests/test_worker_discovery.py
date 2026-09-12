"""
Task 3.4 — Tests for DiscoveryWorkerService (worker_discovery.py).
Verifies the research_stream consumer wires DiscoveryService correctly and
that plan state, child job IDs, and the SERP archive all end up consistent.
"""

import os
from typing import get_args
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from src.application.discovery_service import DiscoveryResult
from src.config_settings import DiscoverySettings, SearchProviderName
from src.domain.models import (
    JobState,
    MessageType,
    QueueMessage,
    ResearchPlan,
    ScrapeJob,
    SearchHit,
)
from src.infrastructure.providers.search_provider import (
    DuckDuckGoSearchProvider,
    NoOpSearchProvider,
    OpenRouterSearchProvider,
    SerperSearchProvider,
)
from src.infrastructure.repositories.research_plan_repository import SqliteResearchPlanRepository
from worker_discovery import PROVIDER_FACTORIES, DiscoveryWorkerService, _build_search_provider


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

    # A real ScrapeJob, not a mock: the worker serialises it into the
    # QueueMessage envelope the scraper deserialises.
    fake_job = ScrapeJob(
        job_id="disc_abc123", url="https://example.com/a", target_site="example.com"
    )

    worker.discovery_service = AsyncMock()
    worker.discovery_service.discover = AsyncMock(
        return_value=DiscoveryResult([fake_job], {}, hits)
    )

    worker.stream_queue = AsyncMock()
    worker.stream_queue.push = AsyncMock()

    worker.artifact_store = AsyncMock()
    worker.artifact_store.store = AsyncMock(return_value="fakesha256")

    with patch("worker_discovery.settings") as mock_settings, \
         patch("worker_discovery.metrics_tracker") as mock_metrics:
        mock_settings.features = {"discovery": True}
        mock_metrics.increment = AsyncMock()
        result = await worker.handle_discovery_query(make_message(plan_id="rp-success"))

    assert result is True
    # The worker archives the hits discover() returned. It used to re-run the
    # search purely to build the SERP artifact, which double-billed metered
    # providers and could archive a SERP that never produced these jobs.
    worker.search_provider.search.assert_not_called()
    # Published onto the stream worker_scraper consumes, not the list queue
    # nothing read (D4). See tests/test_p4_queue_delivery.py for the
    # read-it-back-as-the-scraper-does form of this assertion.
    worker.stream_queue.push.assert_called_once()
    stream, envelope = worker.stream_queue.push.call_args.args
    assert stream == "jobs_stream"
    assert envelope.message_type is MessageType.SCRAPE_JOB
    assert envelope.payload["url"] == "https://example.com/a"
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


# ---------------------------------------------------------------------------
# Provider selection. _build_search_provider used to end in a bare
# `return NoOpSearchProvider()`, so an unrecognised DISCOVERY_SEARCH_PROVIDER
# produced a provider that answers every query with []. Discovery would then
# archive an empty SERP and mark the plan SUCCEEDED — a typo reported as a
# query that matched nothing. These tests pin both halves of the fix: the name
# set cannot drift from the factory table, and neither layer accepts a name it
# cannot build.
# ---------------------------------------------------------------------------


def test_provider_factory_table_matches_the_settings_name_set():
    assert set(get_args(SearchProviderName)) == set(PROVIDER_FACTORIES)


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_search_provider_means_noop(blank):
    """DISCOVERY_SEARCH_PROVIDER= is the obvious way to say "no search". A bare
    Literal would make it a ValidationError at import of config_settings,
    taking down the API and every worker -- not just Discovery."""
    assert DiscoverySettings(search_provider=blank).search_provider == "noop"


@pytest.mark.parametrize("raw", ["DuckDuckGo", " serper ", "NOOP"])
def test_search_provider_tolerates_case_and_whitespace(raw):
    """Matches AISettings.provider rather than diverging from it."""
    assert DiscoverySettings(search_provider=raw).search_provider == raw.strip().lower()


def test_settings_rejects_an_unknown_provider_name():
    with pytest.raises(ValidationError):
        DiscoverySettings(search_provider="serpr")


def test_unknown_provider_raises_instead_of_degrading_to_noop():
    with patch("worker_discovery.settings") as mock_settings:
        mock_settings.discovery.search_provider = "serpr"
        with pytest.raises(ValueError) as excinfo:
            _build_search_provider()

    message = str(excinfo.value)
    assert "serpr" in message
    for name in PROVIDER_FACTORIES:
        assert name in message, "the error must name every valid alternative"


@pytest.mark.parametrize(
    ("name", "expected_type"),
    [
        ("noop", NoOpSearchProvider),
        ("duckduckgo", DuckDuckGoSearchProvider),
        ("serper", SerperSearchProvider),
        ("openrouter", OpenRouterSearchProvider),
    ],
)
def test_each_registered_name_builds_its_own_adapter(name, expected_type):
    with patch("worker_discovery.settings") as mock_settings:
        mock_settings.discovery.search_provider = name
        mock_settings.discovery.search_api_key = None
        mock_settings.discovery.max_fanout = 25
        mock_settings.ai.openrouter_api_key = None
        assert isinstance(_build_search_provider(), expected_type)
