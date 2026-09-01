# Tests for SqliteResearchPlanRepository.

import os
import pytest
from src.infrastructure.repositories.research_plan_repository import SqliteResearchPlanRepository
from src.domain.models import ResearchPlan, JobState
from src.domain.ports import ResearchPlanRepository


@pytest.mark.asyncio
async def test_create_and_get_plan():
    repo = await _make_repo()
    try:
        plan = ResearchPlan(
            plan_id="rp-1",
            query="space launch contracts",
            allowed_domains=["example.com"],
        )
        created = await repo.create_plan(plan)
        assert created.plan_id == "rp-1"

        fetched = await repo.get_plan("rp-1")
        assert fetched is not None
        assert fetched.query == "space launch contracts"
        assert fetched.allowed_domains == ["example.com"]
        assert fetched.state == JobState.QUEUED
        assert fetched.child_job_ids == []
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_get_plan_not_found():
    repo = await _make_repo()
    try:
        result = await repo.get_plan("does-not-exist")
        assert result is None
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_update_plan_state():
    repo = await _make_repo()
    try:
        plan = ResearchPlan(plan_id="rp-state", query="q", allowed_domains=["a.com"])
        await repo.create_plan(plan)

        updated = await repo.update_plan_state("rp-state", JobState.RUNNING)
        assert updated.state == JobState.RUNNING

        updated2 = await repo.update_plan_state("rp-state", JobState.FAILED, error_message="boom")
        assert updated2.state == JobState.FAILED
        assert updated2.error_message == "boom"
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_set_child_job_ids():
    repo = await _make_repo()
    try:
        plan = ResearchPlan(plan_id="rp-children", query="q", allowed_domains=["a.com"])
        await repo.create_plan(plan)

        updated = await repo.set_child_job_ids("rp-children", ["job_1", "job_2"])
        assert updated.child_job_ids == ["job_1", "job_2"]
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_set_serp_artifact_sha():
    repo = await _make_repo()
    try:
        plan = ResearchPlan(plan_id="rp-serp", query="q", allowed_domains=["a.com"])
        await repo.create_plan(plan)

        updated = await repo.set_serp_artifact_sha("rp-serp", "abc123sha")
        assert updated.serp_artifact_sha == "abc123sha"
    finally:
        await _cleanup(repo)


def test_sqlite_adapter_satisfies_research_plan_repository_port():
    """Conformance: SqliteResearchPlanRepository must structurally satisfy the port."""
    repo = SqliteResearchPlanRepository(db_path=":memory:")
    for method in ("create_plan", "get_plan", "update_plan_state", "set_child_job_ids", "set_serp_artifact_sha"):
        assert hasattr(repo, method), f"Missing method: {method}"


async def _make_repo() -> SqliteResearchPlanRepository:
    repo = SqliteResearchPlanRepository(db_path="test_research_plans.db")
    await repo.initialize()
    return repo


async def _cleanup(repo: SqliteResearchPlanRepository):
    await repo.close()
    for suffix in ("", "-wal", "-shm"):
        path = f"test_research_plans.db{suffix}"
        if os.path.exists(path):
            os.remove(path)
