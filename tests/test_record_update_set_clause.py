"""
D15: update_record assigned last_seen twice in one UPDATE.

Both adapters seed the SET clause with `last_seen = <now>` and then *append*
another `last_seen = ...` when the caller passes one explicitly. SQLite accepts
the duplicate and lets the last assignment win, so the SQLite backend happens
to behave correctly by accident. Postgres rejects the statement outright with
42601 "multiple assignments to same column", so the same call that works on the
default backend is an error on the production one.

That asymmetry is the defect: a port is supposed to behave alike on both
backends, and this one only looks fine because the backend most people run
tolerates malformed SQL.

The Postgres half is asserted against the generated statement rather than a
live server -- there is no Postgres in this environment (see
tests/integration/test_postgres_repos.py) and the defect is in the SQL string,
which is fully observable here.
"""

import re
from datetime import UTC, datetime

import pytest

from src.domain.models import ExtractedRecord
from src.infrastructure.repositories.postgres_record_repository import PostgresRecordRepository
from src.infrastructure.repositories.record_repository import SqliteRecordRepository

SET_CLAUSE = re.compile(r"UPDATE records SET (.*?) WHERE ", re.DOTALL)


def _assigned_columns(sql: str) -> list[str]:
    match = SET_CLAUSE.search(sql)
    assert match, f"could not find a SET clause in: {sql!r}"
    return [assignment.split("=")[0].strip() for assignment in match.group(1).split(",")]


class _CapturingConn:
    """Records the UPDATE and answers the get_record that follows it."""

    def __init__(self):
        self.statements: list[str] = []
        self.params: list[tuple] = []

    async def execute(self, sql, *args):
        self.statements.append(sql)
        self.params.append(args)
        return None

    async def fetchrow(self, sql, *args):
        return None

    async def fetch(self, sql, *args):
        return []


@pytest.mark.asyncio
async def test_postgres_never_assigns_the_same_column_twice():
    repo = PostgresRecordRepository("postgresql://unused/unused")
    repo._conn = _CapturingConn()

    await repo.update_record("rec-1", last_seen="2026-03-04T05:06:07+00:00")

    columns = _assigned_columns(repo._conn.statements[0])
    assert len(columns) == len(set(columns)), f"Postgres rejects this with 42601: {columns}"


@pytest.mark.asyncio
async def test_postgres_binds_every_placeholder_it_emits():
    """Rebuilding the clause is the easy half; keeping $n aligned with the
    parameter list is where a rewrite of this shape goes wrong."""
    repo = PostgresRecordRepository("postgresql://unused/unused")
    repo._conn = _CapturingConn()

    await repo.update_record(
        "rec-1", data={"a": 1}, change_type="UPDATED", last_seen="2026-03-04T05:06:07+00:00"
    )

    sql = repo._conn.statements[0]
    placeholders = {int(n) for n in re.findall(r"\$(\d+)", sql)}
    assert placeholders == set(range(1, len(repo._conn.params[0]) + 1)), sql


@pytest.mark.asyncio
async def test_sqlite_never_assigns_the_same_column_twice(tmp_path):
    repo = SqliteRecordRepository(db_path=str(tmp_path / "records.db"))
    await repo.initialize()
    try:
        await repo.create_record(
            ExtractedRecord(record_id="rec-1", source_url="https://example.com/1", data={}),
            job_id="job-1",
        )

        captured: list[str] = []
        original = repo._conn.execute

        def _spy(sql, *args, **kwargs):
            # Deliberately not `async def`: aiosqlite's execute returns an
            # object that is both awaited and used as an async context manager
            # (get_record does the latter). Awaiting it here would hand that
            # caller a plain Cursor and break it.
            captured.append(sql)
            return original(sql, *args, **kwargs)

        repo._conn.execute = _spy
        await repo.update_record("rec-1", last_seen="2026-03-04T05:06:07+00:00")

        updates = [s for s in captured if s.lstrip().upper().startswith("UPDATE")]
        columns = _assigned_columns(updates[0])
        assert len(columns) == len(set(columns)), columns
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_an_explicit_last_seen_still_wins_over_the_default(tmp_path):
    """Deduplicating the clause must keep the caller's value, not the `now()`
    the clause is seeded with -- SQLite's last-assignment-wins behaviour is the
    behaviour being preserved, not discarded."""
    repo = SqliteRecordRepository(db_path=str(tmp_path / "records.db"))
    await repo.initialize()
    try:
        await repo.create_record(
            ExtractedRecord(record_id="rec-1", source_url="https://example.com/1", data={}),
            job_id="job-1",
        )

        explicit = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
        updated = await repo.update_record("rec-1", last_seen=explicit.isoformat())

        assert updated is not None
        assert updated.last_seen == explicit
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_omitting_last_seen_still_touches_it(tmp_path):
    """The seeded assignment is the point of the method for every other
    caller: an update without an explicit timestamp still means "seen now"."""
    repo = SqliteRecordRepository(db_path=str(tmp_path / "records.db"))
    await repo.initialize()
    try:
        stale = datetime(2020, 1, 1, tzinfo=UTC)
        await repo.create_record(
            ExtractedRecord(
                record_id="rec-1", source_url="https://example.com/1", data={},
                first_seen=stale, last_seen=stale,
            ),
            job_id="job-1",
        )

        updated = await repo.update_record("rec-1", change_type="UPDATED")

        assert updated is not None
        assert updated.last_seen > stale
    finally:
        await repo.close()
