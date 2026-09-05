"""
migrate_sqlite_to_postgres.py carries domain_profiles across a cutover.

The table is not one of the SQLAlchemy models the rest of that script moves --
it belongs to the observation repositories, which own their own DDL -- so it
was simply absent from the migrator and every learned profile was dropped on
the way to Postgres.

Only the final write needs a live Postgres. Everything before it -- the table
probe, the schema detection, the read, and the whole dry-run path -- is
covered here, along with the two pure helpers that decide how a legacy row
maps onto the two axes.
"""

import sqlite3
from datetime import UTC, datetime

import pytest

from migrate_sqlite_to_postgres import AVAILABLE_TABLES, DatabaseMigrator

SPLIT_COLUMNS = {"domain", "preferred_fetch_tier", "preferred_extraction_strategy"}
LEGACY_COLUMNS = {"domain", "preferred_strategy"}


def test_split_schema_is_read_directly():
    row = {
        "domain": "example.com",
        "preferred_fetch_tier": "browser",
        "preferred_extraction_strategy": "json_ld",
    }
    assert DatabaseMigrator._profile_axes(row, SPLIT_COLUMNS) == ("browser", "json_ld")


def test_legacy_tier_value_lands_on_the_fetch_axis():
    row = {"domain": "blocked.example.com", "preferred_strategy": "browser"}
    assert DatabaseMigrator._profile_axes(row, LEGACY_COLUMNS) == ("browser", None)


def test_legacy_extractor_value_lands_on_the_extraction_axis():
    """The value that used to be misread as a fetch tier. Carrying 'json_ld'
    into preferred_fetch_tier would re-import the defect the split removed."""
    row = {"domain": "structured.example.com", "preferred_strategy": "json_ld"}
    assert DatabaseMigrator._profile_axes(row, LEGACY_COLUMNS) == ("http", "json_ld")


def test_unrecognised_legacy_value_falls_back_to_the_default_tier():
    row = {"domain": "odd.example.com", "preferred_strategy": "something_else"}
    assert DatabaseMigrator._profile_axes(row, LEGACY_COLUMNS) == ("http", None)


@pytest.mark.parametrize("empty", [None, ""])
def test_never_observed_stays_never_observed(empty):
    """_parse_datetime substitutes now() for a missing value, which would
    claim a domain was just seen when it has never been observed at all."""
    assert DatabaseMigrator._parse_optional_datetime(empty) is None


def test_iso_timestamp_round_trips():
    parsed = DatabaseMigrator._parse_optional_datetime("2026-03-04T05:06:07+00:00")
    assert parsed == datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


def test_unparseable_timestamp_does_not_abort_the_row():
    """One bad timestamp must cost that field, not the whole profile."""
    assert DatabaseMigrator._parse_optional_datetime("not-a-date") is None


def test_domain_profiles_is_an_available_table():
    """migrate_all dispatches only to entries in AVAILABLE_TABLES, so a
    _migrate_domain_profiles that nothing routes to would be dead code.

    This asserts on the gate itself. The first version grepped
    migrate_all's source for "'domain_profiles'" and "_migrate_domain_profiles",
    and both of those strings also occur in the dispatch branch -- so it stayed
    green with the entry deleted from the list that actually decides whether
    the migration runs.
    """
    assert "domain_profiles" in AVAILABLE_TABLES


@pytest.mark.asyncio
async def test_dry_run_reads_the_profiles_and_writes_nothing(tmp_path):
    """Covers the read half of _migrate_domain_profiles without a Postgres.

    The dry-run branch returns before the pool is opened, so this exercises
    the table probe, the PRAGMA, the count and the SELECT -- everything up to
    the write. Previously nothing constructed a DatabaseMigrator at all, and
    replacing the method body with `return` broke no test.
    """
    db = tmp_path / "source.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE domain_profiles ("
        "domain TEXT PRIMARY KEY, preferred_strategy TEXT NOT NULL DEFAULT 'http')"
    )
    conn.execute("INSERT INTO domain_profiles VALUES ('blocked.example.com', 'browser')")
    conn.commit()
    conn.close()

    migrator = DatabaseMigrator(sqlite_path=str(db), dry_run=True)
    migrator._sqlite_conn = sqlite3.connect(db)
    migrator._sqlite_conn.row_factory = sqlite3.Row
    try:
        await migrator._migrate_domain_profiles()
    finally:
        migrator._sqlite_conn.close()

    assert [s.table_name for s in migrator.stats] == ["domain_profiles"]
    assert migrator.stats[0].source_count == 1
    assert migrator.stats[0].inserted == 0


@pytest.mark.asyncio
async def test_source_without_the_table_is_skipped_quietly(tmp_path):
    """A source database that never ran Discovery has no domain_profiles.
    That is a skip, not a failure of the whole migration."""
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()

    migrator = DatabaseMigrator(sqlite_path=str(db), dry_run=True)
    migrator._sqlite_conn = sqlite3.connect(db)
    migrator._sqlite_conn.row_factory = sqlite3.Row
    try:
        await migrator._migrate_domain_profiles()
    finally:
        migrator._sqlite_conn.close()

    assert migrator.stats == []
