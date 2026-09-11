"""
Task 5.1 — Schema migration tests for strategy_observations.
Verifies groundedness/citation_coverage round-trip on a fresh DB, and that
an existing (pre-Phase-5) database gets migrated safely with ALTER TABLE.
"""

import asyncio
import os
import pytest
import aiosqlite

from src.infrastructure.repositories.observation_repository import SqliteObservationRepository
from src.domain.models import StrategyObservation

DB_PATH = "test_obs_migration.db"

# The original 15-column strategy_observations schema, before Phase 5 added
# groundedness/citation_coverage. Shared by the migration tests below.
PRE_PHASE5_OBSERVATIONS_TABLE = """
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
    conn = await aiosqlite.connect(DB_PATH)
    await conn.execute(PRE_PHASE5_OBSERVATIONS_TABLE)
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


# ---------------------------------------------------------------------------
# domain_profiles: preferred_strategy split into two independent axes.
# ---------------------------------------------------------------------------

LEGACY_PROFILES_TABLE = """
CREATE TABLE IF NOT EXISTS domain_profiles (
    domain TEXT PRIMARY KEY,
    preferred_strategy TEXT NOT NULL DEFAULT 'http',
    overlay_id TEXT,
    success_rate REAL NOT NULL DEFAULT 0.0,
    total_observations INTEGER NOT NULL DEFAULT 0,
    avg_latency_ms REAL NOT NULL DEFAULT 0.0,
    block_rate REAL NOT NULL DEFAULT 0.0,
    last_observed TEXT,
    profile_version INTEGER NOT NULL DEFAULT 1,
    throttle_delay_ms REAL NOT NULL DEFAULT 0.0
)
"""


async def _write_legacy_profile(domain: str, preferred_strategy: str) -> None:
    conn = await aiosqlite.connect(DB_PATH)
    await conn.execute(LEGACY_PROFILES_TABLE)
    await conn.execute(
        "INSERT INTO domain_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (domain, preferred_strategy, None, 0.0, 0, 0.0, 0.0, None, 1, 0.0),
    )
    await conn.commit()
    await conn.close()


@pytest.mark.asyncio
async def test_legacy_tier_value_backfills_into_the_fetch_axis():
    _cleanup()
    await _write_legacy_profile("blocked.example.com", "browser")

    repo = SqliteObservationRepository(db_path=DB_PATH)
    try:
        await repo.initialize()
        profile = await repo.get_or_create_profile("blocked.example.com")
        assert profile.preferred_fetch_tier == "browser"
        assert profile.preferred_extraction_strategy is None
    finally:
        await repo.close()
        _cleanup()


@pytest.mark.asyncio
async def test_legacy_extractor_value_backfills_into_the_extraction_axis():
    """The value that used to be read as a fetch tier. It must land on the
    extraction axis and leave the tier at its default, not the other way
    round -- reading 'json_ld' as 'not browser' is the defect being fixed."""
    _cleanup()
    await _write_legacy_profile("structured.example.com", "json_ld")

    repo = SqliteObservationRepository(db_path=DB_PATH)
    try:
        await repo.initialize()
        profile = await repo.get_or_create_profile("structured.example.com")
        assert profile.preferred_extraction_strategy == "json_ld"
        assert profile.preferred_fetch_tier == "http"
    finally:
        await repo.close()
        _cleanup()


@pytest.mark.asyncio
async def test_backfill_does_not_re_run_over_a_relearned_tier():
    """The legacy column is frozen at its pre-split value. Re-running the
    backfill on every boot would copy it back over whatever the fetcher has
    learned since -- reintroducing the overwrite at startup instead of
    hourly."""
    _cleanup()
    await _write_legacy_profile("relearned.example.com", "browser")

    repo = SqliteObservationRepository(db_path=DB_PATH)
    await repo.initialize()
    profile = await repo.get_or_create_profile("relearned.example.com")
    await repo.update_profile(profile.model_copy(update={"preferred_fetch_tier": "http"}))
    await repo.close()

    reopened = SqliteObservationRepository(db_path=DB_PATH)
    try:
        await reopened.initialize()
        profile = await reopened.get_or_create_profile("relearned.example.com")
        assert profile.preferred_fetch_tier == "http"
    finally:
        await reopened.close()
        _cleanup()


@pytest.mark.asyncio
async def test_concurrent_initialize_migrates_a_legacy_database_once():
    """
    boot.py starts the API and the scraper as separate processes against the
    same SQLite file, so several connections reach initialize() at once. Both
    schema migrations probed with PRAGMA table_info outside any transaction,
    and a plain BEGIN is DEFERRED -- it takes no write lock until the first
    write. The loser of the race therefore reached its ALTER after the winner
    had committed and raised "duplicate column name" out of initialize(),
    killing that process during a routine upgrade.
    """
    # Repeated because the race is probabilistic: a single round of four lost
    # the lock roughly 60% of the time, which is how the journal_mode variant
    # of this bug passed in isolation and only failed under full-suite load.
    for _ in range(3):
        _cleanup()
        conn = await aiosqlite.connect(DB_PATH)
        await conn.execute(PRE_PHASE5_OBSERVATIONS_TABLE)
        await conn.execute(LEGACY_PROFILES_TABLE)
        await conn.execute(
            "INSERT INTO domain_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("race.example.com", "browser", None, 0.0, 0, 0.0, 0.0, None, 1, 0.0),
        )
        await conn.commit()
        await conn.close()

        repos = [SqliteObservationRepository(db_path=DB_PATH) for _ in range(4)]
        try:
            results = await asyncio.gather(
                *(repo.initialize() for repo in repos), return_exceptions=True
            )
            failures = [r for r in results if isinstance(r, BaseException)]
            assert not failures, failures

            # Serialising must not mean skipping: the backfill still has to land.
            profile = await repos[0].get_or_create_profile("race.example.com")
            assert profile.preferred_fetch_tier == "browser"
        finally:
            for repo in repos:
                await repo.close()
            _cleanup()
