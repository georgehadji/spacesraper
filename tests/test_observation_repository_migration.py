"""
Task 5.1 — Schema migration tests for strategy_observations.
Verifies groundedness/citation_coverage round-trip on a fresh DB, and that
an existing (pre-Phase-5) database gets migrated safely with ALTER TABLE.
"""

import os
import pytest
import aiosqlite

from src.infrastructure.repositories.observation_repository import SqliteObservationRepository
from src.domain.models import StrategyObservation

DB_PATH = "test_obs_migration.db"


def _cleanup():
    for suffix in ("", "-wal", "-shm"):
        path = f"{DB_PATH}{suffix}"
        if os.path.exists(path):
            os.remove(path)


@pytest.mark.asyncio
async def test_groundedness_and_citation_coverage_round_trip():
    _cleanup()
    repo = SqliteObservationRepository(db_path=DB_PATH)
    try:
        await repo.initialize()

        obs = StrategyObservation(
            observation_id="obs-1",
            job_id="job-1",
            domain="example.com",
            strategy="llm_extract",
            success=True,
            groundedness=0.85,
            citation_coverage=0.5,
        )
        await repo.create_observation(obs)

        fetched = await repo.get_observations(domain="example.com")
        assert len(fetched) == 1
        assert fetched[0].groundedness == 0.85
        assert fetched[0].citation_coverage == 0.5
    finally:
        await repo.close()
        _cleanup()


@pytest.mark.asyncio
async def test_existing_rows_default_to_null_metrics():
    """A non-LLM observation (e.g. strategy='browser') has no groundedness."""
    _cleanup()
    repo = SqliteObservationRepository(db_path=DB_PATH)
    try:
        await repo.initialize()

        obs = StrategyObservation(
            observation_id="obs-2", job_id="job-2", domain="example.com",
            strategy="browser", success=True,
        )
        await repo.create_observation(obs)

        fetched = await repo.get_observations(domain="example.com")
        assert fetched[0].groundedness is None
        assert fetched[0].citation_coverage is None
    finally:
        await repo.close()
        _cleanup()


@pytest.mark.asyncio
async def test_migration_adds_columns_to_pre_phase5_database():
    """
    Simulates a database created before Phase 5: the observations table
    exists WITHOUT groundedness/citation_coverage. initialize() must add
    them via ALTER TABLE without touching existing rows.
    """
    _cleanup()

    # Build a pre-Phase-5 database by hand: original 15-column schema.
    pre_phase5_table = """
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
        created_at TEXT NOT NULL
    )
    """
    conn = await aiosqlite.connect(DB_PATH)
    await conn.execute(pre_phase5_table)
    await conn.execute(
        """INSERT INTO strategy_observations VALUES
           (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("pre-obs-1", "job-old", "old.example.com", "http", None, None,
         1, 1.0, 0.0, 200, 0, 100.0, 0.0, 1, "2026-01-01T00:00:00"),
    )
    await conn.commit()
    await conn.close()

    # Now open it through the repository — must migrate, not fail.
    repo = SqliteObservationRepository(db_path=DB_PATH)
    try:
        await repo.initialize()

        fetched = await repo.get_observations(domain="old.example.com")
        assert len(fetched) == 1
        assert fetched[0].observation_id == "pre-obs-1"
        assert fetched[0].groundedness is None  # migrated column, old row -> NULL

        # New rows after migration must persist the new columns correctly.
        new_obs = StrategyObservation(
            observation_id="post-migration-1", job_id="job-new",
            domain="old.example.com", strategy="llm_extract",
            success=True, groundedness=0.9,
        )
        await repo.create_observation(new_obs)
        fetched2 = await repo.get_observations(domain="old.example.com")
        by_id = {o.observation_id: o for o in fetched2}
        assert by_id["post-migration-1"].groundedness == 0.9
    finally:
        await repo.close()
        _cleanup()


@pytest.mark.asyncio
async def test_migration_is_idempotent():
    """Running initialize() twice (e.g. two workers) must not error."""
    _cleanup()
    repo1 = SqliteObservationRepository(db_path=DB_PATH)
    await repo1.initialize()
    await repo1.close()

    repo2 = SqliteObservationRepository(db_path=DB_PATH)
    try:
        await repo2.initialize()  # must not raise "duplicate column"
    finally:
        await repo2.close()
        _cleanup()
