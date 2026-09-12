# P4 D21: PostgresJobRepository.purge_expired_jobs is not atomic.
#
# self._conn (PostgresConnection) acquires a fresh pooled connection per
# statement, so each DELETE autocommitted on its own. A crash between them left
# job_attempts rows referencing jobs that no longer existed — exactly the state
# the attempts-before-jobs ordering exists to avoid. Parity item with D20:
# same property, opposite backend.
#
# Asserted against a fake asyncpg pool rather than a live server: this repo has
# no Postgres available here, and the defect is entirely in which connection
# the two DELETEs run on, which the fake observes directly.

from datetime import UTC, datetime, timedelta

import pytest


class _FakeTransaction:
    def __init__(self, conn: "_FakeConnection"):
        self._conn = conn

    async def __aenter__(self) -> "_FakeTransaction":
        self._conn.log.append(("BEGIN", self._conn.conn_id))
        self._conn.depth += 1
        return self

    async def __aexit__(self, *exc) -> bool:
        self._conn.depth -= 1
        self._conn.log.append(("COMMIT", self._conn.conn_id))
        return False


class _FakeConnection:
    def __init__(self, conn_id: int, log: list):
        self.conn_id = conn_id
        self.log = log
        self.depth = 0

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    async def execute(self, query: str, *args) -> str:
        verb = query.strip().split()[0].upper()
        table = "job_attempts" if "job_attempts" in query else "jobs"
        self.log.append((f"{verb} {table}", self.conn_id, self.depth))
        return f"{verb} {len(args)}"

    async def fetch(self, query: str, *args) -> list:
        return []


class _FakeAcquire:
    def __init__(self, pool: "_FakePool"):
        self._pool = pool

    async def __aenter__(self) -> _FakeConnection:
        self._pool.next_id += 1
        conn = _FakeConnection(self._pool.next_id, self._pool.log)
        self._pool.handed_out.append(conn)
        return conn

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakePool:
    """One fresh connection per acquire(), exactly like the real pool."""

    def __init__(self):
        self.log: list = []
        self.handed_out: list[_FakeConnection] = []
        self.next_id = 0

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self)


class _PerStatementConnection:
    """Stands in for PostgresConnection — the real one acquires a fresh pooled
    connection for every single statement, which is precisely why the unfixed
    purge autocommits each DELETE on its own."""

    def __init__(self, pool: _FakePool, job_ids: list[str]):
        self._pool = pool
        old = datetime.now(UTC) - timedelta(days=400)
        self._rows = [
            {"job_id": jid, "deleted_at": old, "retention_days": None} for jid in job_ids
        ]

    async def fetch(self, query: str, *args) -> list:
        return self._rows

    async def execute(self, query: str, *args) -> str:
        async with self._pool.acquire() as conn:
            return await conn.execute(query, *args)


def _pg_repo(job_ids: list[str]):
    from src.infrastructure.repositories.postgres_job_repository import PostgresJobRepository

    repo = PostgresJobRepository.__new__(PostgresJobRepository)
    repo._pool = _FakePool()
    repo._conn = _PerStatementConnection(repo._pool, job_ids)
    return repo


@pytest.mark.asyncio
async def test_the_two_purge_deletes_share_one_transaction():
    """Both DELETEs must run on one connection, inside one open transaction."""
    repo = _pg_repo(["j1", "j2"])

    purged = await repo.purge_expired_jobs()

    assert purged == 2
    deletes = [entry for entry in repo._pool.log if entry[0].startswith("DELETE")]
    assert len(deletes) == 2, f"expected two DELETEs, saw {repo._pool.log}"

    conn_ids = {entry[1] for entry in deletes}
    assert len(conn_ids) == 1, (
        f"the two DELETEs ran on {len(conn_ids)} different connections — "
        "each autocommits alone"
    )
    assert all(entry[2] > 0 for entry in deletes), (
        "DELETEs ran outside any open transaction"
    )
    assert [e[0] for e in repo._pool.log][0] == "BEGIN"


@pytest.mark.asyncio
async def test_attempts_are_still_deleted_before_their_jobs():
    """Control: Postgres enforces the job_attempts -> jobs foreign key, so the
    order inside the transaction still matters."""
    repo = _pg_repo(["j1"])

    await repo.purge_expired_jobs()

    ordered = [entry[0] for entry in repo._pool.log if entry[0].startswith("DELETE")]
    assert ordered == ["DELETE job_attempts", "DELETE jobs"]


@pytest.mark.asyncio
async def test_nothing_expired_opens_no_transaction():
    """Control: the empty case must not acquire a connection at all."""
    repo = _pg_repo([])

    assert await repo.purge_expired_jobs() == 0
    assert repo._pool.log == []
