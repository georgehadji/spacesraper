# Regression test for SqliteObservationRepository.create_evaluation, which
# had 13 SQL placeholders for 14 bound values and raised
# sqlite3.ProgrammingError on every real call — caught while writing the
# Postgres mirror (postgres_observation_repository.py), not by any prior
# test; this file had none before.

import os

import pytest

from src.domain.models import EvaluationResult
from src.infrastructure.repositories.observation_repository import SqliteObservationRepository

DB_PATH = "test_observation_repository.db"


@pytest.fixture
async def repo():
    r = SqliteObservationRepository(db_path=DB_PATH)
    await r.initialize()
    yield r
    await r.close()
    for suffix in ("", "-wal", "-shm"):
        path = f"{DB_PATH}{suffix}"
        if os.path.exists(path):
            os.remove(path)


@pytest.mark.asyncio
async def test_create_evaluation_does_not_raise(repo):
    result = EvaluationResult(
        evaluation_id="eval-1",
        candidate_strategy="candidate-overlay-1",
        domain="example.com",
        sample_size=10,
        precision=0.9,
        completeness=0.8,
        latency_p50=100.0,
        latency_p95=250.0,
        cost_per_record=0.01,
        block_rate=0.0,
        score=0.85,
        recommendation="PROMOTE",
    )

    stored = await repo.create_evaluation(result)

    assert stored.evaluation_id == "eval-1"
