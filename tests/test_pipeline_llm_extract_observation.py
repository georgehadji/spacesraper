"""
Task 5.3 — Tests for llm_extract observation recording in DataPipeline.
Verifies the ExplorationPolicy gate, the recorded strategy/groundedness,
and that a missing/failing observation_repo never breaks extraction.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.application.pipeline import DataPipeline
from src.domain.models import Opportunity
from src.domain.models import StrategyObservation


def make_opportunity(**overrides) -> Opportunity:
    defaults = dict(
        source="test_source",
        source_url="https://example.com/opp/1",
        external_id="OPP-1",
        title="Original Title procurement notice",
        buyer="Original Buyer Agency",
        url="https://example.com/opp/1",
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


class AlwaysExplore:
    def should_explore(self, domain):
        return True


class NeverExplore:
    def should_explore(self, domain):
        return False


class FakeEnrichmentProvider:
    def __init__(self, enrich_result):
        self._result = enrich_result

    async def is_available(self):
        return True

    async def enrich(self, data, prompt_hint=""):
        return self._result


@pytest.mark.asyncio
async def test_exploration_gate_skips_enrichment_and_recording():
    """When ExplorationPolicy says not to explore, no enrich() call, no observation."""
    provider = FakeEnrichmentProvider({"title_en": "Translated Title"})
    obs_repo = AsyncMock()

    pipeline = DataPipeline(
        ai_enrichment_enabled=True,
        enrichment_provider=provider,
        exploration_policy=NeverExplore(),
        observation_repo=obs_repo,
    )

    entity = make_opportunity()
    await pipeline._enrich_opportunity(entity, job_id="job-1")

    assert entity.title == "Original Title procurement notice"  # unchanged
    obs_repo.create_observation.assert_not_called()


@pytest.mark.asyncio
async def test_successful_enrichment_records_llm_extract_observation():
    provider = FakeEnrichmentProvider({"title_en": "Original Title procurement notice"})
    obs_repo = AsyncMock()

    pipeline = DataPipeline(
        ai_enrichment_enabled=True,
        enrichment_provider=provider,
        exploration_policy=AlwaysExplore(),
        observation_repo=obs_repo,
    )

    entity = make_opportunity()
    await pipeline._enrich_opportunity(entity, job_id="job-2")

    obs_repo.create_observation.assert_called_once()
    recorded: StrategyObservation = obs_repo.create_observation.call_args[0][0]
    assert recorded.strategy == "llm_extract"
    assert recorded.job_id == "job-2"
    assert recorded.domain == "test_source"
    assert recorded.success is True
    # Claim text is a verbatim echo of the source title -> fully grounded
    assert recorded.groundedness == 1.0


@pytest.mark.asyncio
async def test_ungrounded_enrichment_scores_low_groundedness():
    """Enrichment that invents unrelated content must score low groundedness."""
    provider = FakeEnrichmentProvider({"summary": "Completely unrelated fabricated content here"})
    obs_repo = AsyncMock()

    pipeline = DataPipeline(
        ai_enrichment_enabled=True,
        enrichment_provider=provider,
        exploration_policy=AlwaysExplore(),
        observation_repo=obs_repo,
    )

    entity = make_opportunity()
    await pipeline._enrich_opportunity(entity, job_id="job-3")

    recorded: StrategyObservation = obs_repo.create_observation.call_args[0][0]
    assert recorded.groundedness == 0.0


@pytest.mark.asyncio
async def test_failed_enrichment_records_unsuccessful_observation():
    provider = FakeEnrichmentProvider(None)  # enrich() returns None (failure)
    obs_repo = AsyncMock()

    pipeline = DataPipeline(
        ai_enrichment_enabled=True,
        enrichment_provider=provider,
        exploration_policy=AlwaysExplore(),
        observation_repo=obs_repo,
    )

    entity = make_opportunity()
    await pipeline._enrich_opportunity(entity, job_id="job-4")

    recorded: StrategyObservation = obs_repo.create_observation.call_args[0][0]
    assert recorded.success is False
    assert recorded.groundedness == 1.0  # no claims -> vacuously grounded


@pytest.mark.asyncio
async def test_no_observation_repo_never_raises():
    """Default observation_repo=None must not break extraction."""
    provider = FakeEnrichmentProvider({"title_en": "New Title"})

    pipeline = DataPipeline(
        ai_enrichment_enabled=True,
        enrichment_provider=provider,
        exploration_policy=AlwaysExplore(),
    )

    entity = make_opportunity()
    await pipeline._enrich_opportunity(entity, job_id="job-5")  # must not raise
    assert entity.title == "New Title"


@pytest.mark.asyncio
async def test_observation_repo_failure_does_not_break_extraction():
    """A broken observation_repo must be swallowed, not propagated."""
    provider = FakeEnrichmentProvider({"title_en": "New Title"})
    obs_repo = AsyncMock()
    obs_repo.create_observation = AsyncMock(side_effect=Exception("db down"))

    pipeline = DataPipeline(
        ai_enrichment_enabled=True,
        enrichment_provider=provider,
        exploration_policy=AlwaysExplore(),
        observation_repo=obs_repo,
    )

    entity = make_opportunity()
    await pipeline._enrich_opportunity(entity, job_id="job-6")  # must not raise
    assert entity.title == "New Title"


@pytest.mark.asyncio
async def test_default_exploration_policy_is_five_percent():
    """Constructing without an explicit exploration_policy uses the shared 5% default."""
    from src.application.exploration_policy import DEFAULT_EXPLORATION_RATE

    pipeline = DataPipeline(ai_enrichment_enabled=True)
    assert pipeline.exploration_policy.exploration_rate == DEFAULT_EXPLORATION_RATE
