"""
What migrate_sqlite_to_postgres.py *claims* it did, versus what it wrote.

The migration report is the only evidence an operator has that a cutover
succeeded -- the source database is usually decommissioned on the strength of
it. Three ways it lied:

  D3  opportunities counted a row as migrated before the INSERT ran, and
      swallowed the failure. Postgres aborts the whole transaction on a failed
      statement, so the *rest* of the batch failed too while the summary still
      reported a full copy.
  D25 dead letters got a fresh uuid4 per row with no conflict clause, so
      re-running after a partial failure -- normal operator behaviour --
      duplicated the entire table instead of resuming.
  D26 an unknown --tables name was filtered out silently: a typo migrated
      nothing and still printed a success report.

The fake session below models the one behaviour that makes D3 more than a
miscount: once a statement errors, every later statement on that connection
errors too. A loop that logs and continues is not resilient, it is blind.
"""

import uuid

import pytest

from migrate_sqlite_to_postgres import AVAILABLE_TABLES, DatabaseMigrator


class _FakeResult:
    def __init__(self, value=None, rowcount=1):
        self._value = value
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._value


class _RecordingSession:
    """Duck-typed AsyncSession: just enough for the upsert loops.

    `aborted` is the important part. Postgres refuses every subsequent
    statement on a connection whose transaction failed, so a migrator that
    keeps going after an error is not recovering -- it is generating a second
    error per remaining row and then failing at commit anyway.
    """

    def __init__(self, *, fail_on_insert: int | None = None, existing_ids=()):
        self.fail_on_insert = fail_on_insert
        self.existing_ids = set(existing_ids)
        self.selects = 0
        self.inserts = 0
        self.commits = 0
        self.aborted = False

    async def execute(self, stmt):
        rendered = str(stmt).lstrip().upper()
        if self.aborted:
            raise RuntimeError("current transaction is aborted, commands ignored until end of transaction block")
        if rendered.startswith("SELECT"):
            self.selects += 1
            wanted = next(iter(stmt.compile().params.values()), None)
            return _FakeResult(object() if wanted in self.existing_ids else None)
        if self.fail_on_insert == self.inserts:
            self.inserts += 1
            self.aborted = True
            raise RuntimeError("simulated constraint violation")
        self.inserts += 1
        return _FakeResult()

    async def commit(self):
        if self.aborted:
            raise RuntimeError("cannot commit an aborted transaction")
        self.commits += 1


def _opportunity(id_: str) -> dict:
    return {"id": id_, "source": "test", "title": "t", "url": id_}


# --- D3: counts describe rows that landed -----------------------------------


@pytest.mark.asyncio
async def test_a_failed_insert_aborts_the_batch_instead_of_silently_truncating_it():
    """The row after the failure cannot succeed -- its transaction is already
    dead. The old loop attempted it anyway, logged a second error, and left
    the summary claiming every row migrated."""
    migrator = DatabaseMigrator(dry_run=False)
    session = _RecordingSession(fail_on_insert=1)
    batch = [_opportunity("a"), _opportunity("b"), _opportunity("c")]

    with pytest.raises(RuntimeError, match="simulated constraint violation"):
        await migrator._upsert_opportunities_batch(session, batch)

    assert session.inserts == 2, "stopped at the failure; row 'c' was never attempted"
    assert session.commits == 0, "nothing may be committed out of an aborted transaction"


@pytest.mark.asyncio
async def test_the_failure_the_operator_sees_is_the_one_that_happened():
    """With the only row failing, the old code returned inserted=1 and then
    died at commit -- so `pytest.raises(RuntimeError)` alone would pass against
    the defect. What has to change is *which* error escapes: the constraint
    violation that explains the problem, not a commit failure downstream of a
    swallowed one."""
    migrator = DatabaseMigrator(dry_run=False)
    session = _RecordingSession(fail_on_insert=0)

    with pytest.raises(RuntimeError) as raised:
        await migrator._upsert_opportunities_batch(session, [_opportunity("only")])

    assert "simulated constraint violation" in str(raised.value)
    assert "commit" not in str(raised.value)
    assert session.commits == 0


@pytest.mark.asyncio
async def test_a_clean_batch_still_separates_inserts_from_updates():
    migrator = DatabaseMigrator(dry_run=False)
    session = _RecordingSession(existing_ids={"b"})

    inserted, updated = await migrator._upsert_opportunities_batch(
        session, [_opportunity("a"), _opportunity("b")]
    )

    assert (inserted, updated) == (1, 1)
    assert session.inserts == 2
    assert session.commits == 1


# --- D25: a re-run resumes, it does not duplicate ---------------------------


def test_the_same_dead_letter_row_always_gets_the_same_id():
    """uuid4() per row meant every re-run appended the whole table again.
    Re-running a migration after a failure is routine; it has to be safe."""
    row = {
        "job_id": "job-7",
        "url": "https://example.com/a",
        "created_at": "2026-01-01T00:00:00",
        "error_message": "timeout",
    }
    assert DatabaseMigrator._dead_letter_id(row) == DatabaseMigrator._dead_letter_id(dict(row))


def test_two_different_dead_letters_do_not_collide():
    first = {"job_id": "job-7", "url": "https://example.com/a", "created_at": "2026-01-01T00:00:00"}
    second = {"job_id": "job-7", "url": "https://example.com/b", "created_at": "2026-01-01T00:00:00"}
    assert DatabaseMigrator._dead_letter_id(first) != DatabaseMigrator._dead_letter_id(second)


def test_a_dead_letter_id_is_a_uuid():
    """The target column is UUID, not TEXT -- a str here fails at insert time."""
    row = {"job_id": "j", "url": "u", "created_at": "2026-01-01T00:00:00"}
    assert isinstance(DatabaseMigrator._dead_letter_id(row), uuid.UUID)


def test_a_source_row_with_its_own_id_keys_off_that():
    """When the source table has a primary key, that is the natural key --
    it survives an error_message being rewritten between runs."""
    keyed = {"id": "dlq-1", "job_id": "j", "url": "u", "error_message": "first wording"}
    reworded = {"id": "dlq-1", "job_id": "j", "url": "u", "error_message": "second wording"}
    assert DatabaseMigrator._dead_letter_id(keyed) == DatabaseMigrator._dead_letter_id(reworded)


# --- D26: a flag that does nothing is worse than an absent one --------------


def test_an_unknown_table_name_is_refused_rather_than_filtered_away():
    with pytest.raises(ValueError, match="opportunitys"):
        DatabaseMigrator._resolve_tables(["opportunitys"])


def test_the_error_names_what_the_tool_can_actually_move():
    with pytest.raises(ValueError, match="dead_letters"):
        DatabaseMigrator._resolve_tables(["nope"])


def test_a_known_subset_is_kept():
    assert DatabaseMigrator._resolve_tables(["runs", "opportunities"]) == [
        "opportunities",
        "runs",
    ]


def test_no_selection_means_every_known_table():
    assert DatabaseMigrator._resolve_tables(None) == list(AVAILABLE_TABLES)


@pytest.mark.asyncio
async def test_verification_reports_a_failed_check_as_a_nonzero_exit(monkeypatch):
    """--verify parsed and was never read: the operator asked for a check,
    got no check, and got a success exit code."""
    import verify_migration as verifier

    async def _fake_verify():
        return [verifier.VerificationResult("runs", "row count", False, "12 != 41")]

    monkeypatch.setattr(verifier, "verify_migration", _fake_verify)

    import migrate_sqlite_to_postgres as migrate

    assert await migrate._run_verification() != 0


@pytest.mark.asyncio
async def test_verification_passing_exits_zero(monkeypatch):
    import verify_migration as verifier

    async def _fake_verify():
        return [verifier.VerificationResult("runs", "row count", True, "41 == 41")]

    monkeypatch.setattr(verifier, "verify_migration", _fake_verify)

    import migrate_sqlite_to_postgres as migrate

    assert await migrate._run_verification() == 0
