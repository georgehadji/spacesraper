"""
migrate_sqlite_to_postgres.py carries domain_profiles across a cutover.

The table is not one of the SQLAlchemy models the rest of that script moves --
it belongs to the observation repositories, which own their own DDL -- so it
was simply absent from the migrator and every learned profile was dropped on
the way to Postgres. The write path needs a live Postgres; the two pure
helpers below are where the actual decisions are made, and they do not.
"""

from datetime import UTC, datetime

import pytest

from migrate_sqlite_to_postgres import DatabaseMigrator

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
    """The dispatch in migrate_all only runs tables listed there, so a
    _migrate_domain_profiles that nothing routes to would be dead code."""
    import inspect

    source = inspect.getsource(DatabaseMigrator.migrate_all)
    assert "'domain_profiles'" in source
    assert "_migrate_domain_profiles" in source
