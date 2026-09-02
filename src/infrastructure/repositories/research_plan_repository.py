# SQLite adapter for ResearchPlanRepository port.
# Mirrors job_repository.py's shape: aiosqlite with WAL mode for concurrent reads.

import json
import logging
from datetime import datetime, timezone
from typing import Optional, List

import aiosqlite

from src.domain.models import ResearchPlan, JobState

logger = logging.getLogger("Spacescraper.ResearchPlanRepository")

CREATE_RESEARCH_PLANS_TABLE = """
CREATE TABLE IF NOT EXISTS research_plans (
    plan_id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    max_results INTEGER NOT NULL DEFAULT 10,
    allowed_domains TEXT NOT NULL DEFAULT '[]',
    serp_artifact_sha TEXT,
    state TEXT NOT NULL DEFAULT 'QUEUED',
    child_job_ids TEXT NOT NULL DEFAULT '[]',
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

CREATE_RESEARCH_PLANS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_research_plans_state ON research_plans(state)",
    "CREATE INDEX IF NOT EXISTS idx_research_plans_created ON research_plans(created_at)",
]


class SqliteResearchPlanRepository:
    """SQLite-backed implementation of ResearchPlanRepository."""

    def __init__(self, db_path: str = "spacescraper_jobs.db"):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def initialize(self):
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute(CREATE_RESEARCH_PLANS_TABLE)
        for idx in CREATE_RESEARCH_PLANS_INDEXES:
            await self._conn.execute(idx)
        await self._conn.commit()
        logger.info("Research plan repository initialized at %s", self.db_path)

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def create_plan(self, plan: ResearchPlan) -> ResearchPlan:
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO research_plans
               (plan_id, query, max_results, allowed_domains, serp_artifact_sha,
                state, child_job_ids, error_message, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                plan.plan_id, plan.query, plan.max_results,
                json.dumps(plan.allowed_domains), plan.serp_artifact_sha,
                plan.state.value, json.dumps(plan.child_job_ids),
                plan.error_message,
                plan.created_at.isoformat(), plan.updated_at.isoformat(),
            ),
        )
        await self._conn.commit()
        return plan

    async def get_plan(self, plan_id: str) -> Optional[ResearchPlan]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM research_plans WHERE plan_id = ?", (plan_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return self._row_to_plan(row) if row else None

    async def update_plan_state(
        self, plan_id: str, new_state: JobState, *, error_message: Optional[str] = None
    ) -> Optional[ResearchPlan]:
        assert self._conn is not None
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "UPDATE research_plans SET state = ?, error_message = ?, updated_at = ? WHERE plan_id = ?",
            (new_state.value, error_message, now, plan_id),
        )
        await self._conn.commit()
        return await self.get_plan(plan_id)

    async def set_child_job_ids(self, plan_id: str, child_job_ids: List[str]) -> Optional[ResearchPlan]:
        assert self._conn is not None
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "UPDATE research_plans SET child_job_ids = ?, updated_at = ? WHERE plan_id = ?",
            (json.dumps(child_job_ids), now, plan_id),
        )
        await self._conn.commit()
        return await self.get_plan(plan_id)

    async def set_serp_artifact_sha(self, plan_id: str, sha256: str) -> Optional[ResearchPlan]:
        assert self._conn is not None
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "UPDATE research_plans SET serp_artifact_sha = ?, updated_at = ? WHERE plan_id = ?",
            (sha256, now, plan_id),
        )
        await self._conn.commit()
        return await self.get_plan(plan_id)

    @staticmethod
    def _row_to_plan(row) -> ResearchPlan:
        return ResearchPlan(
            plan_id=row["plan_id"],
            query=row["query"],
            max_results=row["max_results"],
            allowed_domains=json.loads(row["allowed_domains"]),
            serp_artifact_sha=row["serp_artifact_sha"],
            state=JobState(row["state"]),
            child_job_ids=json.loads(row["child_job_ids"]),
            error_message=row["error_message"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
