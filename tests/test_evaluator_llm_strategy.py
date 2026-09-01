"""
Task 5.3 exit criterion: "An LLM strategy appears in EvaluationResult rows
with a recommendation." Proves StrategyEvaluator needs zero changes to score
llm_extract, because StrategyObservation.strategy was already a free string.
"""

import os
import pytest

from src.application.evaluator import StrategyEvaluator
from src.infrastructure.repositories.observation_repository import SqliteObservationRepository
from src.domain.models import StrategyObservation

DB_PATH = "test_evaluator_llm.db"


def _cleanup():
    for suffix in ("", "-wal", "-shm"):
        path = f"{DB_PATH}{suffix}"
        if os.path.exists(path):
            os.remove(path)


async def _make_repo() -> SqliteObservationRepository:
    _cleanup()
    repo = SqliteObservationRepository(db_path=DB_PATH)
    await repo.initialize()
    return repo


@pytest.mark.asyncio
async def test_llm_extract_strategy_is_scored_and_recommended():
    repo = await _make_repo()
    try:
        # Baseline: http strategy, mediocre performance
        for i in range(6):
            await repo.create_observation(StrategyObservation(
                observation_id=f"obs-http-{i}", job_id=f"job-{i}",
                domain="example.com", strategy="http",
                valid_record_count=1, required_field_completeness=0.5,
                success=True, latency_ms=500.0,
            ))

        # Candidate: llm_extract, strong groundedness and completeness
        for i in range(6):
            await repo.create_observation(StrategyObservation(
                observation_id=f"obs-llm-{i}", job_id=f"job-llm-{i}",
                domain="example.com", strategy="llm_extract",
                valid_record_count=1, required_field_completeness=0.95,
                success=True, latency_ms=300.0, groundedness=0.9,
            ))

        evaluator = StrategyEvaluator(repo)
        result = await evaluator.evaluate_strategy(
            domain="example.com", candidate_strategy="llm_extract", baseline_strategy="http",
        )

        assert result is not None
        assert result.candidate_strategy == "llm_extract"
        assert result.recommendation in ("promote", "demote", "no_change")
        assert result.sample_size == 6
    finally:
        await repo.close()
        _cleanup()


@pytest.mark.asyncio
async def test_llm_extract_below_min_observations_returns_none():
    """Fewer than MIN_OBSERVATIONS_FOR_EVALUATION observations -> no premature verdict."""
    repo = await _make_repo()
    try:
        await repo.create_observation(StrategyObservation(
            observation_id="obs-llm-solo", job_id="job-1",
            domain="example.com", strategy="llm_extract",
            success=True, groundedness=0.9,
        ))

        evaluator = StrategyEvaluator(repo)
        result = await evaluator.evaluate_strategy(
            domain="example.com", candidate_strategy="llm_extract",
        )
        assert result is None
    finally:
        await repo.close()
        _cleanup()
