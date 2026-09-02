# Shared connection machinery for the Postgres repository adapters (C8/W5.3,
# R-W1).
#
# Two distinct connection shapes live here, for two distinct needs:
#
# PostgresConnection is what every repository holds as self._conn for its
# whole lifetime. Each execute/fetchrow/fetch independently acquires a
# connection from the pool, runs its one statement, and releases it back —
# so it is safe for the concurrent access that a single bare
# asyncpg.Connection was not (R1): every FastAPI request handler and all
# three of main.py's background tasks (strategy_selector, outbox_relay,
# job_reaper) can now query through the same repo object at once without
# `InterfaceError: another operation is in progress`. No repository method
# needed to change to get this — they all just call self._conn.execute(...)
# exactly as before.
#
# PostgresTransaction is the exception: the F14 unit of work in main.py
# needs job_repo.create_job() and outbox_repo.create_event() to share one
# real transaction, so a failure between them rolls back the first instead
# of orphaning a job with no outbox event. job_repo.transaction() acquires
# ONE connection from the pool and holds it — with a real
# `asyncpg.Connection.transaction()` open — for the life of the `async with`
# block; create_job/create_event accept that transaction via conn= instead
# of going through self._conn's per-call pool acquisition.
#
# aiosqlite's equivalent needs none of this: a single aiosqlite.Connection
# already serializes concurrent callers through its own background-thread
# queue (unlike a bare asyncpg.Connection), and already holds an implicit
# transaction open across statements until commit()/rollback() — that's what
# lets SqliteJobRepository.transaction() just yield self._conn unchanged.

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

logger = logging.getLogger("Spacescraper.PostgresConn")

# How long to wait for the TCP+auth handshake, how long any single query may
# run before asyncpg gives up on it, and how many times to retry pool/connect
# creation (with a doubling backoff) before giving up entirely. A freshly
# started `postgres:16-alpine` in docker-compose/k8s routinely isn't
# accepting connections yet when the app's own startup races it.
CONNECT_TIMEOUT_S = 10.0
COMMAND_TIMEOUT_S = 30.0
CONNECT_RETRY_ATTEMPTS = 5
CONNECT_RETRY_BASE_DELAY_S = 0.5

# Pool sizing defaults, used when the caller doesn't have
# DatabaseSettings.pool_size/max_overflow handy. min_size stays small — most
# of this app's traffic is bursty background-task polling, not a steady
# stream that benefits from pre-warmed connections.
DEFAULT_POOL_MIN_SIZE = 2
DEFAULT_POOL_MAX_SIZE = 20


class PostgresConnection:
    """Pool-backed facade: every execute/fetchrow/fetch acquires a pool
    connection for just that call. See module docstring."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def execute(self, query: str, *args: Any) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetch(self, query: str, *args: Any) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def close(self) -> None:
        await self._pool.close()


class PostgresTransaction:
    """One connection, acquired from the pool and held for the life of a
    `job_repo.transaction()` block, with a real transaction already open on
    it. Unlike PostgresConnection, every call here runs on the SAME
    connection — that's what makes the writes through it atomic together."""

    def __init__(self, conn: asyncpg.Connection):
        self._raw = conn

    async def execute(self, query: str, *args: Any) -> Any:
        return await self._raw.execute(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        return await self._raw.fetchrow(query, *args)

    async def fetch(self, query: str, *args: Any) -> Any:
        return await self._raw.fetch(query, *args)


@asynccontextmanager
async def transaction_scope(pool: asyncpg.Pool) -> AsyncIterator["PostgresTransaction"]:
    """Acquires one pool connection, opens a real transaction on it, and
    yields a PostgresTransaction bound to that connection. Commits on clean
    exit, rolls back (via asyncpg's own transaction context manager) if the
    block raises — mirrors PostgresJobRepository.transaction()'s only
    caller, main.py's F14 unit of work."""
    async with pool.acquire() as raw_conn, raw_conn.transaction():
        yield PostgresTransaction(raw_conn)


def asyncpg_dsn(url: str) -> str:
    """asyncpg doesn't understand SQLAlchemy's '+asyncpg' dialect suffix."""
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def create_pool_with_retry(
    dsn: str, *, min_size: int = DEFAULT_POOL_MIN_SIZE, max_size: int = DEFAULT_POOL_MAX_SIZE,
) -> asyncpg.Pool:
    """asyncpg.create_pool() with a connect timeout, a per-query command
    timeout, and a bounded retry with doubling backoff. Every Postgres
    repository's initialize() goes through this instead of a bare
    asyncpg.create_pool(self.dsn) — see CONNECT_TIMEOUT_S's comment for why."""
    delay = CONNECT_RETRY_BASE_DELAY_S
    last_error: Exception | None = None
    for attempt in range(1, CONNECT_RETRY_ATTEMPTS + 1):
        try:
            return await asyncpg.create_pool(
                dsn, min_size=min_size, max_size=max_size,
                timeout=CONNECT_TIMEOUT_S, command_timeout=COMMAND_TIMEOUT_S,
            )
        except (OSError, TimeoutError, asyncpg.PostgresError) as e:
            last_error = e
            if attempt == CONNECT_RETRY_ATTEMPTS:
                break
            logger.warning(
                "Postgres pool creation attempt %d/%d failed (%s), retrying in %.1fs",
                attempt, CONNECT_RETRY_ATTEMPTS, e, delay,
            )
            await asyncio.sleep(delay)
            delay *= 2
    raise ConnectionError(
        f"Could not create Postgres pool after {CONNECT_RETRY_ATTEMPTS} attempts"
    ) from last_error
