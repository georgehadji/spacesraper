# Postgres adapter for learning/observation storage (C8/W5.3).
# Mirrors SqliteObservationRepository — see postgres_job_repository.py's
# module docstring for the single-connection design rationale.

import json
import logging
from datetime import datetime
from typing import Any

import asyncpg

from src.config_settings import settings
from src.domain.models import DomainProfile, EvaluationResult, FeedbackItem, StrategyObservation
from src.infrastructure.repositories.postgres_conn import PostgresConnection, asyncpg_dsn, create_pool_with_retry

logger = logging.getLogger("Spacescraper.PostgresObservationRepository")

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
    blocked BOOLEAN NOT NULL DEFAULT FALSE,
    latency_ms REAL NOT NULL DEFAULT 0.0,
    cost REAL NOT NULL DEFAULT 0.0,
    success BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL
)
"""

CREATE_FEEDBACK_TABLE = """
CREATE TABLE IF NOT EXISTS feedback_items (
    feedback_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    corrected_data TEXT,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL
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
    created_at TIMESTAMPTZ NOT NULL
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
    last_observed TIMESTAMPTZ,
    profile_version INTEGER NOT NULL DEFAULT 1,
    throttle_delay_ms REAL NOT NULL DEFAULT 0.0
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_obs_domain ON strategy_observations(domain)",
    "CREATE INDEX IF NOT EXISTS idx_obs_job ON strategy_observations(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_obs_strategy ON strategy_observations(strategy)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_record ON feedback_items(record_id)",
    "CREATE INDEX IF NOT EXISTS idx_eval_domain ON evaluation_results(domain)",
]


class PostgresObservationRepository:
    """Postgres adapter for observations, feedback, evaluations, and profiles."""

    def __init__(self, dsn: str):
        self.dsn = asyncpg_dsn(dsn)
        self._pool: asyncpg.Pool | None = None
        self._conn: PostgresConnection | None = None

    async def initialize(self) -> None:
        self._pool = await create_pool_with_retry(
            self.dsn, min_size=2, max_size=settings.database.pool_size + settings.database.max_overflow,
        )
        self._conn = PostgresConnection(self._pool)
        for table in (CREATE_OBSERVATIONS_TABLE, CREATE_FEEDBACK_TABLE,
                      CREATE_EVALUATIONS_TABLE, CREATE_PROFILES_TABLE):
            await self._conn.execute(table)
        for idx in INDEXES:
            await self._conn.execute(idx)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            self._conn = None

    async def create_observation(self, obs: StrategyObservation) -> StrategyObservation:
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO strategy_observations VALUES
               ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)""",
            obs.observation_id, obs.job_id, obs.domain, obs.strategy, obs.overlay_id,
            obs.input_fingerprint, obs.valid_record_count, obs.required_field_completeness,
            obs.duplicate_rate, obs.http_status, obs.blocked, obs.latency_ms,
            obs.cost, obs.success, obs.created_at,
        )
        return obs

    async def get_observations(
        self, domain: str | None = None, strategy: str | None = None,
        limit: int = 100, offset: int = 0,
    ) -> list[StrategyObservation]:
        assert self._conn is not None
        where = []
        params: list[Any] = []
        if domain:
            params.append(domain)
            where.append(f"domain = ${len(params)}")
        if strategy:
            params.append(strategy)
            where.append(f"strategy = ${len(params)}")
        clause = " AND ".join(where) if where else "1=1"
        params.extend([limit, offset])
        # `clause` is built only from the fixed literals above, never from caller
        # input; all values are bound via $n params.
        rows = await self._conn.fetch(
            f"SELECT * FROM strategy_observations WHERE {clause} "  # nosec B608
            f"ORDER BY created_at DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}",
            *params,
        )
        return [self._row_to_obs(r) for r in rows]

    async def create_feedback(self, fb: FeedbackItem) -> FeedbackItem:
        assert self._conn is not None
        await self._conn.execute(
            "INSERT INTO feedback_items VALUES ($1, $2, $3, $4, $5, $6, $7)",
            fb.feedback_id, fb.record_id, fb.job_id, fb.decision,
            json.dumps(fb.corrected_data) if fb.corrected_data else None,
            fb.reason, fb.created_at,
        )
        return fb

    async def create_evaluation(self, ev: EvaluationResult) -> EvaluationResult:
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO evaluation_results VALUES
               ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)""",
            ev.evaluation_id, ev.candidate_strategy, ev.baseline_strategy, ev.domain,
            ev.sample_size, ev.precision, ev.completeness, ev.latency_p50, ev.latency_p95,
            ev.cost_per_record, ev.block_rate, ev.score, ev.recommendation,
            ev.created_at,
        )
        return ev

    async def get_or_create_profile(self, domain: str) -> DomainProfile:
        """R-W6.3: mirrors SqliteObservationRepository.get_or_create_profile —
        INSERT ... ON CONFLICT DO NOTHING closes the same TOCTOU race Postgres
        would otherwise raise as an unhandled UniqueViolationError for."""
        assert self._conn is not None
        profile = DomainProfile(domain=domain)
        row = await self._conn.fetchrow(
            """INSERT INTO domain_profiles VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
               ON CONFLICT (domain) DO NOTHING RETURNING *""",
            domain, profile.preferred_strategy, profile.overlay_id, profile.success_rate,
            profile.total_observations, profile.avg_latency_ms, profile.block_rate,
            None, profile.profile_version, profile.throttle_delay_ms,
        )
        if row:
            return self._row_to_profile(row)
        row = await self._conn.fetchrow("SELECT * FROM domain_profiles WHERE domain = $1", domain)
        return self._row_to_profile(row)

    async def update_profile(self, profile: DomainProfile) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """UPDATE domain_profiles SET preferred_strategy=$1, overlay_id=$2, success_rate=$3,
               total_observations=$4, avg_latency_ms=$5, block_rate=$6, last_observed=$7,
               profile_version=$8, throttle_delay_ms=$9 WHERE domain=$10""",
            profile.preferred_strategy, profile.overlay_id, profile.success_rate,
            profile.total_observations, profile.avg_latency_ms, profile.block_rate,
            profile.last_observed,
            profile.profile_version + 1, profile.throttle_delay_ms, profile.domain,
        )

    @staticmethod
    def _row_to_obs(row: Any) -> StrategyObservation:
        return StrategyObservation(
            observation_id=row["observation_id"], job_id=row["job_id"],
            domain=row["domain"], strategy=row["strategy"],
            overlay_id=row["overlay_id"],
            input_fingerprint=row["input_fingerprint"],
            valid_record_count=row["valid_record_count"],
            required_field_completeness=row["required_field_completeness"],
            duplicate_rate=row["duplicate_rate"],
            http_status=row["http_status"],
            blocked=row["blocked"],
            latency_ms=row["latency_ms"], cost=row["cost"],
            success=row["success"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_profile(row: Any) -> DomainProfile:
        return DomainProfile(
            domain=row["domain"],
            preferred_strategy=row["preferred_strategy"],
            overlay_id=row["overlay_id"],
            success_rate=row["success_rate"],
            total_observations=row["total_observations"],
            avg_latency_ms=row["avg_latency_ms"],
            block_rate=row["block_rate"],
            last_observed=row["last_observed"],
            profile_version=row["profile_version"],
            throttle_delay_ms=row["throttle_delay_ms"],
        )
