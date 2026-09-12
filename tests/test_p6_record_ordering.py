"""P6 — RecordRepository.list_records honours the ordering it documents (D27).

The port says "Records are ordered by created_at ASC". Both implementations
ordered by record_id ASC, which is a random hex suffix — so the documented
chronological order was never delivered, and the idx_records_job_created
index on (job_id, created_at), which exists precisely for this query, was
dead.

The plan offered two honest resolutions: correct the docstring, or honour it.
Honouring it, as recommended: the index's existence is the original intent,
and chronological order is what a consumer paging a job's records wants. The
cursor becomes composite — "<created_at>|<record_id>" — because created_at
alone is not unique and paging on a non-unique key skips or repeats rows.

Scope: SQLite only here. Postgres's list_records is changed identically in
the same commit and is covered by tests/integration/test_postgres_repos.py,
which needs a live server this environment does not have.
"""

import pytest

from src.domain.models import ExtractedRecord
from src.infrastructure.repositories.record_repository import SqliteRecordRepository

JOB = "job-order"


async def repo_with_chronology(tmp_path, pairs) -> SqliteRecordRepository:
    """Insert records, then stamp created_at so chronology is explicit.

    created_at is a DB default with one-second resolution, so records written
    inside one test would otherwise all share a timestamp and prove nothing
    about ordering.
    """
    repo = SqliteRecordRepository(db_path=str(tmp_path / "records.db"))
    await repo.initialize()
    for record_id, created_at in pairs:
        await repo.create_record(
            ExtractedRecord(
                record_id=record_id,
                record_type="tender",
                data={"n": record_id},
                source_url=f"https://example.invalid/{record_id}",
            ),
            job_id=JOB,
        )
        await repo._conn.execute(
            "UPDATE records SET created_at = ? WHERE record_id = ?",
            (created_at, record_id),
        )
    await repo._conn.commit()
    return repo


# record_id order is deliberately the reverse of created_at order, so the two
# possible answers are distinguishable.
REVERSED_CHRONOLOGY = [
    ("rec_dddd", "2026-09-01 10:00:00"),
    ("rec_cccc", "2026-09-01 11:00:00"),
    ("rec_bbbb", "2026-09-01 12:00:00"),
    ("rec_aaaa", "2026-09-01 13:00:00"),
]


@pytest.mark.asyncio
async def test_a_records_come_back_in_the_order_the_port_promises(tmp_path):
    """The guard for D27."""
    repo = await repo_with_chronology(tmp_path, REVERSED_CHRONOLOGY)
    try:
        records, _ = await repo.list_records(JOB, limit=10)
    finally:
        await repo.close()

    assert [r.record_id for r in records] == [
        "rec_dddd", "rec_cccc", "rec_bbbb", "rec_aaaa",
    ], "records came back in record_id order, not the documented created_at order"


@pytest.mark.asyncio
async def test_b_paging_preserves_that_order_without_gaps_or_repeats(tmp_path):
    """Chronological order has to survive the cursor, not just page one."""
    repo = await repo_with_chronology(tmp_path, REVERSED_CHRONOLOGY)
    try:
        seen = []
        cursor = None
        for _ in range(10):
            page, cursor = await repo.list_records(JOB, cursor=cursor, limit=2)
            seen.extend(r.record_id for r in page)
            if cursor is None:
                break
    finally:
        await repo.close()

    assert seen == ["rec_dddd", "rec_cccc", "rec_bbbb", "rec_aaaa"]


@pytest.mark.asyncio
async def test_c_records_sharing_a_timestamp_are_neither_skipped_nor_repeated(tmp_path):
    """Why the cursor is composite.

    created_at is not unique — a batch written in the same second shares one.
    A cursor on created_at alone either re-serves the whole second or skips
    the rest of it, depending on the comparison used.
    """
    same_second = [
        ("rec_1", "2026-09-01 10:00:00"),
        ("rec_2", "2026-09-01 10:00:00"),
        ("rec_3", "2026-09-01 10:00:00"),
        ("rec_4", "2026-09-01 10:00:01"),
    ]
    repo = await repo_with_chronology(tmp_path, same_second)
    try:
        seen = []
        cursor = None
        for _ in range(10):
            page, cursor = await repo.list_records(JOB, cursor=cursor, limit=2)
            seen.extend(r.record_id for r in page)
            if cursor is None:
                break
    finally:
        await repo.close()

    assert sorted(seen) == ["rec_1", "rec_2", "rec_3", "rec_4"]
    assert len(seen) == len(set(seen)), "a record was served on two pages"
    assert seen[-1] == "rec_4", "the later second must still sort last"


@pytest.mark.asyncio
async def test_d_an_unusable_cursor_is_rejected_rather_than_silently_restarting(tmp_path):
    """A cursor that cannot be read must not quietly re-serve page one."""
    repo = await repo_with_chronology(tmp_path, REVERSED_CHRONOLOGY)
    try:
        with pytest.raises(ValueError):
            await repo.list_records(JOB, cursor="rec_legacy_format", limit=2)
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_e_the_api_turns_an_unusable_cursor_into_a_client_error(tmp_path):
    """That ValueError reaches the HTTP boundary, which owns request shape.

    The cursor is a caller-supplied query parameter, so a malformed one is a
    400, not the 500 an unhandled ValueError would produce.
    """
    from fastapi import HTTPException

    import main
    from src.domain.models import Job

    class _JobRepo:
        async def get_job(self, job_id):
            return Job(job_id=job_id, url="https://example.invalid/a", target_site="universal")

    class _RecordRepo:
        async def list_records(self, job_id, *, cursor=None, limit=50):
            raise ValueError("unrecognised cursor")

        async def get_record_count(self, job_id):
            return 0

    with pytest.raises(HTTPException) as exc:
        await main.get_job_records(
            job_id="job-order",
            cursor="rec_legacy_format",
            limit=50,
            auth=("key", None),
            job_repo=_JobRepo(),
            record_repo=_RecordRepo(),
        )

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_f_an_empty_job_still_pages_cleanly(tmp_path):
    """Control."""
    repo = SqliteRecordRepository(db_path=str(tmp_path / "records.db"))
    await repo.initialize()
    try:
        records, cursor = await repo.list_records("nothing-here")
    finally:
        await repo.close()

    assert records == [] and cursor is None
