# ADR 0001: Implement a real Postgres backend for C8, on a single connection per repo

**Status:** Accepted, decision 2 superseded 2026-09-02 (see update below)
**Date:** 2026-08-22
**Related:** [Architecture Remediation Plan](../plans/2026-08-10-architecture-remediation-to-8.5.md), finding C8, workstream W5.3

**Update, 2026-09-02 (R-W1, [Review Remediation Plan](../plans/2026-09-01-review-remediation.md)):**
Decision 2's premise — that Postgres "matches the SQLite adapters' existing concurrency
ceiling" — turned out to be false. `aiosqlite.Connection` serializes concurrent callers
through its own background-thread queue; a bare `asyncpg.Connection` has no such property,
and concurrent awaits on it raise `InterfaceError`. Under the single-connection design, one
process-wide repo instance serving every FastAPI request plus three always-running
background tasks (`strategy_selector`, `outbox_relay`, `job_reaper`) hit this as a real,
not hypothetical, bug (finding R1). R-W1 implemented exactly the follow-up this ADR
predicted: `PostgresConnection` is now pool-backed (`asyncpg.Pool`, via
`create_pool_with_retry`), and `JobRepository.transaction()` — a
`transaction()`-context-manager port both backends implement — replaced `main.py`'s direct
`job_repo._conn` access for the F14 unit of work. Decisions 1 and 3 are unaffected.

## Context

All five persistence repositories (`JobRepository`, `RecordRepository`, `OutboxRepository`,
`OverlayRepository`, `ObservationRepository`) hardcoded `aiosqlite` against one SQLite file
(`spacescraper_jobs.db`). `docker-compose.enterprise.yml` set `DB_URL` for a Postgres service
on every app container, and `src/config_settings.py` had a `DatabaseSettings.url` field for it —
but nothing in the app ever read either. `src/database_models.py` held SQLAlchemy ORM models,
but they were shaped for the pre-W2.3 `Opportunity` entity (`opportunities`, `runs`,
`dead_letters`, `event_logs` tables) and don't match the current domain model
(`Job`, `ExtractedRecord`, `OutboxEvent`, `ExtractionOverlay`, `StrategyObservation`) at all —
they were already dead in two senses: unreferenced, and wrong-shaped even if wired up.

The plan's own text framed this as a binary: implement real `PostgresXRepository` siblings
against the existing `DatabaseSettings`/`database_models.py`, or delete the whole aspirational
surface (`DatabaseSettings`, `database_models.py`, `asyncpg`, `sqlalchemy[asyncio]`,
`migrate_sqlite_to_postgres.py`, `verify_migration.py`) and stop advertising Postgres in the
enterprise manifest. Given the choice, implementing was picked over deleting.

## Decision

1. **Five new repository adapters**, one per SQLite sibling, each implementing the same
   `src.domain.ports` Protocol: `PostgresJobRepository`, `PostgresRecordRepository`,
   `PostgresOutboxRepository`, `PostgresOverlayRepository`, `PostgresObservationRepository`
   (`src/infrastructure/repositories/postgres_*_repository.py`). They use `asyncpg` directly
   against hand-written SQL — the same style as the SQLite adapters — not
   `database_models.py`'s SQLAlchemy ORM, because that ORM's schema no longer matches the
   domain model. `database_models.py` stays unused; it is a candidate for deletion in a
   follow-up (see Consequences).

2. **Single persistent connection per repo, not a pool.** The SQLite adapters share one
   `aiosqlite.Connection` per repo for the life of the process — that's what lets `main.py`'s
   F14 unit-of-work reach into `job_repo._conn` directly to commit a job insert and an outbox
   insert as one transaction. A real `asyncpg.Pool` would need a `transaction()`-context-manager
   port both backends implement, and a rewrite of that one call site in `main.py` to stop
   reaching into `_conn`. That's real, correctly-scoped follow-up work, but a strictly bigger
   change with more test surface than "swap the backend, keep behavior identical" — so this
   round matches the SQLite adapters' existing concurrency ceiling (already noted in the plan
   itself: "five separate SQLite connections to one file is already the concurrency ceiling")
   rather than trying to lift it in the same change. `PostgresConnection`
   (`src/infrastructure/repositories/postgres_conn.py`) wraps a bare `asyncpg.Connection` to
   reproduce aiosqlite's "hold a transaction open across statements until commit()/rollback()"
   behavior on `commit=False` calls, so F14's guarantee holds under Postgres too.

3. **Explicit backend switch, not DSN-presence sniffing.** `DatabaseSettings.url` is a
   `PostgresDsn` with a default value even when `DB_URL` isn't set in the environment, so its
   mere presence can't signal intent to use Postgres. `src/infrastructure/repositories/factory.py`
   reads `PERSISTENCE_BACKEND` (`sqlite` by default, `postgres` opt-in) and returns the right
   concrete repo per call. `docker-compose.enterprise.yml`'s seven app containers now set
   `PERSISTENCE_BACKEND=postgres` alongside their existing `DB_URL` line — closing the actual
   C8 complaint ("sets DB_URL for a Postgres the app never reads").

4. **`src/bootstrap.py`'s `AppContainer`** (the single composition root, shared by `main.py`
   and both worker entrypoints) now types its repo fields against the `src.domain.ports`
   Protocols instead of concrete `Sqlite*` classes, and builds them via the factory functions —
   so the backend choice is made in exactly one place.

## Consequences

- **No live Postgres validated this locally.** Docker Desktop's daemon wasn't running in the
  environment this was written in, so the new repos have not been exercised against a real
  Postgres server — only syntax-checked and covered by the existing SQLite-backed test suite
  (unaffected, since it never touches the new code paths unless `PERSISTENCE_BACKEND=postgres`
  is set). CI does not currently run a Postgres service either. Until one of those exists,
  treat the Postgres path as implemented-but-unverified-against-a-real-server — the query and
  schema logic was hand-translated from the tested SQLite originals (placeholder style
  `?` → `$n`, `cursor.rowcount` → `RETURNING` + `fetchrow`), but hand-translation is exactly the
  kind of change most likely to have a subtle bug a real integration test would catch.
- **`database_models.py` is still unused.** It wasn't deleted in this change because deleting
  it wasn't explicitly asked for, and it's a separate, low-risk cleanup (verify zero importers,
  remove). `migrate_sqlite_to_postgres.py` and `verify_migration.py` both import it and are
  themselves written against the stale `Opportunity`-era schema — neither will produce a
  correct migration against the current schema without a rewrite. That rewrite (and the
  `database_models.py` deletion) is scoped as follow-up, not done here.
- **Bug found in passing:** `SqliteObservationRepository.create_evaluation()` had 13 SQL
  placeholders for 14 bound values (`evaluation_results` has 14 columns) — every real call
  raised `sqlite3.ProgrammingError`, uncaught because the method had zero test coverage. Found
  while hand-translating this method to `PostgresObservationRepository` and fixed at the root
  in both the SQLite original and the new Postgres sibling; a regression test now covers it
  (`tests/test_observation_repository.py`).
- **Real pooling remains future work.** If Postgres concurrency ever becomes a real bottleneck
  (multiple API replicas hammering one connection per repo), the `transaction()`-abstraction
  path outlined in point 2 is the next step — not a from-scratch redesign.
