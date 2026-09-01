# SQLite adapter for learning/observation storage.

import json
import logging
from datetime import datetime
from typing import Optional, List

import aiosqlite

from src.domain.models import StrategyObservation, FeedbackItem, EvaluationResult, DomainProfile

logger = logging.getLogger("Spacescraper.ObservationRepository")

CREATE_OBSERVATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS strategy_observations (
    observation_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    strategy TEXT NOT NULL,
    overlay_id TEXT,
    input_fingerprint TEXT,
    valid_record_count INTEGER NOT NULL DEFAULT 0,
    required_field_completeness REAL NOT NULL DEFAULT 0.0,
    duplicate_rate REAL NOT NULL DEFAULT 0.0,
    http_status INTEGER,
    blocked INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0.0,
    cost REAL NOT NULL DEFAULT 0.0,
    success INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    groundedness REAL,
    citation_coverage REAL
)
"""

# Nullable columns added after the initial release. ALTER TABLE ADD COLUMN is
# safe and non-locking in SQLite; existing rows get NULL for both. Applied
# defensively in initialize() since CREATE TABLE IF NOT EXISTS is a no-op
# against a database that predates these columns.
_MIGRATION_COLUMNS = [
    ("groundedness", "REAL"),
    ("citation_coverage", "REAL"),
]

CREATE_FEEDBACK_TABLE = """
CREATE TABLE IF NOT EXISTS feedback_items (
    feedback_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    corrected_data TEXT,
    reason TEXT,
    created_at TEXT NOT NULL
)
"""

CREATE_EVALUATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS evaluation_results (
    evaluation_id TEXT PRIMARY KEY,
    candidate_strategy TEXT NOT NULL,
    baseline_strategy TEXT NOT NULL DEFAULT 'active',
    domain TEXT NOT NULL,
    sample_size INTEGER NOT NULL DEFAULT 0,
    precision REAL NOT NULL DEFAULT 0.0,
    completeness REAL NOT NULL DEFAULT 0.0,
    latency_p50 REAL NOT NULL DEFAULT 0.0,
    latency_p95 REAL NOT NULL DEFAULT 0.0,
    cost_per_record REAL NOT NULL DEFAULT 0.0,
    block_rate REAL NOT NULL DEFAULT 0.0,
    score REAL NOT NULL DEFAULT 0.0,
    recommendation TEXT,
    created_at TEXT NOT NULL
)
"""

CREATE_PROFILES_TABLE = """
CREATE TABLE IF NOT EXISTS domain_profiles (
    domain TEXT PRIMARY KEY,
    preferred_strategy TEXT NOT NULL DEFAULT 'http',
    overlay_id TEXT,
    success_rate REAL NOT NULL DEFAULT 0.0,
    total_observations INTEGER NOT NULL DEFAULT 0,
    avg_latency_ms REAL NOT NULL DEFAULT 0.0,
    block_rate REAL NOT NULL DEFAULT 0.0,
    last_observed TEXT,
    profile_version INTEGER NOT NULL DEFAULT 1
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_obs_domain ON strategy_observations(domain)",
    "CREATE INDEX IF NOT EXISTS idx_obs_job ON strategy_observations(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_obs_strategy ON strategy_observations(strategy)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_record ON feedback_items(record_id)",
    "CREATE INDEX IF NOT EXISTS idx_eval_domain ON evaluation_results(domain)",
]


class SqliteObservationRepository:
    """SQLite adapter for observations, feedback, evaluations, and profiles."""

    def __init__(self, db_path: str = "spacescraper_jobs.db"):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def initialize(self):
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        for table in [CREATE_OBSERVATIONS_TABLE, CREATE_FEEDBACK_TABLE,
                       CREATE_EVALUATIONS_TABLE, CREATE_PROFILES_TABLE]:
            await self._conn.execute(table)
        await self._migrate_observation_columns()
        for idx in INDEXES:
            await self._conn.execute(idx)
        await self._conn.commit()

    async def _migrate_observation_columns(self):
        """Add any missing nullable columns to strategy_observations (Task 5.1)."""
        assert self._conn is not None
        async with self._conn.execute("PRAGMA table_info(strategy_observations)") as cursor:
            existing = {row["name"] for row in await cursor.fetchall()}
        for name, col_type in _MIGRATION_COLUMNS:
            if name not in existing:
                await self._conn.execute(
                    f"ALTER TABLE strategy_observations ADD COLUMN {name} {col_type}"
                )
                logger.info("Migrated strategy_observations: added column %s", name)

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def create_observation(self, obs: StrategyObservation) -> StrategyObservation:
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO strategy_observations VALUES
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (obs.observation_id, obs.job_id, obs.domain, obs.strategy, obs.overlay_id,
             obs.input_fingerprint, obs.valid_record_count, obs.required_field_completeness,
             obs.duplicate_rate, obs.http_status, int(obs.blocked), obs.latency_ms,
             obs.cost, int(obs.success), obs.created_at.isoformat(),
             obs.groundedness, obs.citation_coverage),
        )
        await self._conn.commit()
        return obs

    async def get_observations(
        self, domain: Optional[str] = None, strategy: Optional[str] = None,
        limit: int = 100, offset: int = 0,
    ) -> List[StrategyObservation]:
        assert self._conn is not None
        where = []
        params = []
        if domain:
            where.append("domain = ?"); params.append(domain)
        if strategy:
            where.append("strategy = ?"); params.append(strategy)
        clause = " AND ".join(where) if where else "1=1"
        async with self._conn.execute(
            f"SELECT * FROM strategy_observations WHERE {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_obs(r) for r in rows]

    async def create_feedback(self, fb: FeedbackItem) -> FeedbackItem:
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO feedback_items VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (fb.feedback_id, fb.record_id, fb.job_id, fb.decision,
             json.dumps(fb.corrected_data) if fb.corrected_data else None,
             fb.reason, fb.created_at.isoformat()),
        )
        await self._conn.commit()
        return fb

    async def create_evaluation(self, ev: EvaluationResult) -> EvaluationResult:
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO evaluation_results VALUES
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ev.evaluation_id, ev.candidate_strategy, ev.baseline_strategy, ev.domain,
             ev.sample_size, ev.precision, ev.completeness, ev.latency_p50, ev.latency_p95,
             ev.cost_per_record, ev.block_rate, ev.score, ev.recommendation,
             ev.created_at.isoformat()),
        )
        await self._conn.commit()
        return ev

    async def get_or_create_profile(self, domain: str) -> DomainProfile:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM domain_profiles WHERE domain = ?", (domain,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return self._row_to_profile(row)
        profile = DomainProfile(domain=domain)
        await self._conn.execute(
            "INSERT INTO domain_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (domain, profile.preferred_strategy, profile.overlay_id, profile.success_rate,
             profile.total_observations, profile.avg_latency_ms, profile.block_rate,
             None, profile.profile_version),
        )
        await self._conn.commit()
        return profile

    async def update_profile(self, profile: DomainProfile) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """UPDATE domain_profiles SET preferred_strategy=?, overlay_id=?, success_rate=?,
               total_observations=?, avg_latency_ms=?, block_rate=?, last_observed=?,
               profile_version=? WHERE domain=?""",
            (profile.preferred_strategy, profile.overlay_id, profile.success_rate,
             profile.total_observations, profile.avg_latency_ms, profile.block_rate,
             profile.last_observed.isoformat() if profile.last_observed else None,
             profile.profile_version + 1, profile.domain),
        )
        await self._conn.commit()

    @staticmethod
    def _row_to_obs(row) -> StrategyObservation:
        return StrategyObservation(
            observation_id=row["observation_id"], job_id=row["job_id"],
            domain=row["domain"], strategy=row["strategy"],
            overlay_id=row["overlay_id"],
            input_fingerprint=row["input_fingerprint"],
            valid_record_count=row["valid_record_count"],
            required_field_completeness=row["required_field_completeness"],
            duplicate_rate=row["duplicate_rate"],
            http_status=row["http_status"],
            blocked=bool(row["blocked"]),
            latency_ms=row["latency_ms"], cost=row["cost"],
            success=bool(row["success"]),
            groundedness=row["groundedness"] if "groundedness" in row.keys() else None,
            citation_coverage=row["citation_coverage"] if "citation_coverage" in row.keys() else None,
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_profile(row) -> DomainProfile:
        return DomainProfile(
            domain=row["domain"],
            preferred_strategy=row["preferred_strategy"],
            overlay_id=row["overlay_id"],
            success_rate=row["success_rate"],
            total_observations=row["total_observations"],
            avg_latency_ms=row["avg_latency_ms"],
            block_rate=row["block_rate"],
            last_observed=datetime.fromisoformat(row["last_observed"]) if row["last_observed"] else None,
            profile_version=row["profile_version"],
        )
