# Review Remediation Plan — Uncommitted P3/W5.3 Work

**Date:** 2026-09-01
**Branch at time of writing:** `fix/e2e-correctness-and-headless-cli`
**Source:** Code review pass over the 25 uncommitted files (12 modified, 1 deleted, 12 untracked) carrying the P3 session work and the W5.3 Postgres backend
**Status:** All eight workstreams (R-W1 through R-W8) complete and verified. One item —
`main.py`/`src/bootstrap.py`'s ~145 pre-existing mypy errors — was surveyed during R-W7.1
and deliberately left open as a scoped-out follow-up rather than attempted; see R-W7.1 for
the evidence behind that call. Everything else in the finding register (R1–R16) is closed.
Full unit suite: 496/496. Nothing has been committed — all work remains in the uncommitted
working tree, same as when this plan started.

This plan covers only the **uncommitted** working-tree changes. It does not cover the
`master` merge conflict, which is separate and larger work — see
[Relationship to the master merge](#7-relationship-to-the-master-merge).

---

## Table of Contents

- [0. Exit Criteria](#0-exit-criteria)
- [1. Corrections to the Review](#1-corrections-to-the-review)
- [2. Finding Register](#2-finding-register)
- [3. Workstreams](#3-workstreams)
  - [R-W1 — Connection Lifecycle and Unit of Work](#r-w1--connection-lifecycle-and-unit-of-work)
  - [R-W2 — Complete the Repository Contracts](#r-w2--complete-the-repository-contracts)
  - [R-W3 — Session Lease Correctness](#r-w3--session-lease-correctness)
  - [R-W4 — Proxy Credentials as a Domain Value](#r-w4--proxy-credentials-as-a-domain-value)
  - [R-W5 — Postgres Schema Types](#r-w5--postgres-schema-types)
  - [R-W6 — Configuration and Operational Hardening](#r-w6--configuration-and-operational-hardening)
  - [R-W7 — Close the Verification Gaps](#r-w7--close-the-verification-gaps)
  - [R-W8 — Dead Code and Drift](#r-w8--dead-code-and-drift)
- [4. Sequencing](#4-sequencing)
- [5. Risk Register](#5-risk-register)
- [6. Explicitly Out of Scope](#6-explicitly-out-of-scope)
- [7. Relationship to the master merge](#7-relationship-to-the-master-merge)

---

## 0. Exit Criteria

1. The Postgres backend survives concurrent load. Two in-flight queries no longer raise
   `InterfaceError`.
2. `job_repo._conn` is not reachable from `main.py`. The F14 unit-of-work guarantee is
   expressed through the port, not around it.
3. Every method declared on a `src/domain/ports.py` Protocol is implemented by every
   adapter claiming to satisfy it, and a test enforces this.
4. `JobReaper.purge_once()` actually purges. Retention is a working feature on both
   backends.
5. A leased `Session` is released exactly once, and two concurrent jobs on one domain
   cannot overwrite each other's outcome.
6. An authenticated proxy works identically on the HTTP tier and the Chromium tier.
7. `mypy --strict` covers the composition root, not only `src/domain`.

---

## 1. Corrections to the Review

Two findings from the review pass were wrong or understated. Recorded here rather than
quietly dropped.

**Withdrawn.** The review flagged `worker_scraper.py:429` for exceeding a 120-character
line limit. `pyproject.toml` sets `line-length = 120`, but `[tool.ruff.lint].ignore`
contains `E501` with the comment "line length — not a correctness signal". Line length is
deliberately not enforced in this project. Not a finding.

**Understated.** The review reported `purge_expired_jobs` as missing on
`PostgresJobRepository`, implying the SQLite backend was fine. It is missing on **both**
adapters. `src/domain/ports.py:114` declares it, `src/application/reaper.py:43` calls it
from a background task that runs in every deployment, and neither `job_repository.py` nor
`postgres_job_repository.py` implements it. `reaper.py`'s
`except Exception: logger.exception(...)` has been swallowing the resulting
`AttributeError` once per `purge_interval` (86400s) since the reaper was written.
Retention purge has therefore never worked on any backend. Severity raised to CRITICAL and
rescoped from "new Postgres gap" to "live production bug".

---

## 2. Finding Register

Severity scale matches the architecture remediation plan: CRITICAL blocks merge, HIGH
should block merge, MEDIUM is a maintainability concern.

| ID | Finding | Severity | Evidence | Status |
|---|---|---|---|---|
| R1 | Every Postgres adapter opens a bare `asyncpg.connect()`, not a pool. `asyncpg.Connection` is not safe for concurrent use. One process-wide `AppContainer` serves every FastAPI request plus three always-running background tasks, so two concurrent queries raise `InterfaceError: another operation is in progress` | CRITICAL | `postgres_conn.py:21`; `asyncpg.connect` at `postgres_{job,record,outbox,overlay,observation}_repository.py:{72,50,46,60,99}`; `bootstrap.py:75`; `main.py:91,94,96` | Open — R-W1 |
| R2 | `purge_expired_jobs` is declared on the `JobRepository` port and called by `JobReaper.purge_once()`, but implemented by neither adapter. The `AttributeError` is swallowed by the reaper's blanket handler, so retention silently never runs | CRITICAL | `ports.py:114`; `reaper.py:43`; absent from both `job_repository.py` and `postgres_job_repository.py`; `reaper.py:64-65` | Open — R-W2 |
| R3 | `browser_session` can be released twice. The success-path release at `worker_scraper.py:430` runs before `_finalize_success()`, which is unguarded; if it raises, the `except Exception` handler at 482 releases the same session again from a stale snapshot. The comment there ("the earlier release() never ran") is false on that path | HIGH | `worker_scraper.py:428-432`, `448`, `482-487` | Open — R-W3 |
| R4 | `SessionPool.lease()` does not mark a session in-use. `DomainRateLimiter(default_budget=2)` permits two concurrent jobs per domain, so both lease the same `Session` object; each later calls `release()` with its own stale copy, and `bucket[i] = updated` makes the last writer win, discarding the other's outcome | HIGH | `sessions.py:27-42`, `44-56`; `worker_scraper.py:93` | Open — R-W3 |
| R5 | `main.py` calls `job_repo._conn`, `create_job(commit=...)` and `record_repo.get_record_count()` through port-typed variables that declare none of them. The abstraction `bootstrap.py` claims ("any backend satisfying the contract works") is not true — the real contract is wider than the Protocol | HIGH | `main.py:350,352,359,362,364,479`; `ports.py` JobRepository/RecordRepository; `outbox_relay.py:137` still typed `SqliteOutboxRepository` | Open — R-W1 |
| R6 | Proxy credentials embedded in the URL (`http://user:pass@host:port`) are passed to Playwright as `{"server": <whole string>}`. Playwright expects `username`/`password` as separate keys. The HTTP tier (curl_cffi) parses embedded userinfo natively; the Chromium tier does not, so the two tiers diverge on the same proxy string | HIGH | `worker_scraper.py:379`; `provider.py` docstring; `browser/pool.py:197-211`; `browser/engine.py:71-84` | Open — R-W4 |
| R7 | `soft_delete_job`, `soft_delete_record` and `purge_expired_records` are declared on ports and implemented by neither adapter. Dormant (no caller today), unlike R2 | HIGH | `ports.py:101,149,153` | Open — R-W2 |
| R8 | `PostgresRecordRepository.create_record(record, job_id="")` diverges from the port's `create_record(record)`. A caller written against the declared signature silently persists an orphan with `job_id=""` that no `list_records`/`get_record_count` query will ever return. Same divergence on the SQLite adapter | HIGH | `ports.py:122`; `postgres_record_repository.py:61`; `record_repository.py:65` | Open — R-W2 |
| R9 | Every Postgres table stores timestamps as `TEXT` and booleans as `INTEGER`, mirroring SQLite's forced types instead of using native `timestamptz`/`boolean`. `now()::text` also emits a different format than the application's `.isoformat()` writes, so the two sort inconsistently | MEDIUM | `postgres_job_repository.py:22-43`; `postgres_observation_repository.py:16-80`; `postgres_record_repository.py:31` | Open — R-W5 |
| R10 | `DatabaseSettings.url` defaults to `postgresql+asyncpg://postgres:postgres@localhost:5432/spacescraper`. An operator who sets `PERSISTENCE_BACKEND=postgres` and forgets `DB_URL` silently connects to localhost with default credentials instead of failing | MEDIUM | `config_settings.py:17-19`; `factory.py:16` | Open — R-W6 |
| R11 | `pool_size`, `max_overflow` and `pool_pre_ping` are defined on `DatabaseSettings` and read by nothing, because there is no pool. They advertise behaviour that does not exist | MEDIUM | `config_settings.py:21-23` | Open — R-W6 (resolved by R-W1) |
| R12 | `asyncpg.connect()` calls have no `timeout` or `command_timeout` and no startup retry. A Postgres container not yet accepting connections fails the app's startup instead of being waited out, and a stalled query hangs the shared connection indefinitely | MEDIUM | all five `initialize()` methods | Open — R-W6 |
| R13 | `get_or_create_profile` does check-then-insert with no `ON CONFLICT`, so concurrent callers race the `domain_profiles` primary key and raise an unhandled unique violation. Present on both backends | MEDIUM | `postgres_observation_repository.py:169-181`; same shape in `observation_repository.py` | Open — R-W6 |
| R14 | `list_schemas()` and `list_overlays()` have no `LIMIT`, unlike their paginated `list_jobs`/`list_records` siblings | MEDIUM | `postgres_overlay_repository.py:90-93,137-145`; `overlay_repository.py:104-110,162-175` | Open — R-W6 |
| R15 | `ExplorationPolicy` has zero callers anywhere in `src/`. This diff fixes a real sampling bug in it (deterministic Beta-mean where `random.betavariate` was intended), but the fix has no runtime effect, and `record_outcome` is a `pass` stub | MEDIUM | `exploration_policy.py:122-144`; grep across `src/` returns no caller | Open — R-W8 |
| R16 | `StaticProxyProvider` has no test. Round-robin wraparound, empty-list handling and `SCRAPER_PROXY_LIST` parsing are all untested, on the file that replaced the deleted `manager.py` | MEDIUM | `provider.py`; `tests/test_sessions.py` uses a `FakeProxyProvider` instead | Open — R-W7 |

### Verified clean

- No SQL injection. Every query across all five Postgres adapters is parameterized. The
  only dynamic fragments (`update_attempt`, `update_record`, `get_observations`) join
  fixed literal strings from hardcoded branches, never caller text, and carry correct
  `# nosec B608` annotations.
- No hardcoded secrets, no credentials in logs. `sessions.py` logs `session_id`, domain,
  score and use count, never `session.proxy`.
- SSRF posture unchanged. `validate_outbound_url` still gates job submission
  (`main.py:328`) and the Tier-1 fetch (`http_fetcher.py:35`). The proxy plumbing adds no
  new SSRF surface: proxy addresses come from operator-controlled `SCRAPER_PROXY_LIST`,
  never from user input.
- Deleting `proxies/manager.py` is safe. Nothing in the tree imports it, and
  `proxies/__init__.py` re-exports nothing. Its only real behaviour was round-robin
  selection, which `StaticProxyProvider` reproduces; its `get_session_cookies` /
  `save_session_cookies` were `return []` / `pass` stubs.
- Both fetch tiers are time-bounded. Tier 1 via `FetchRequest.timeout_s` (20s default),
  Tier 2 via `engine.timeout` (35s) applied to `goto`, `wait_for_selector` and
  `wait_for_load_state`.
- `tests/integration/test_postgres_repos.py` runs against a real `postgres:16-alpine`
  service in CI, not mocks. It covers optimistic-concurrency version conflicts, cursor
  pagination, outbox lifecycle, overlay promotion and the F14 rollback. Its gaps are
  concurrency and the reaper purge path, both addressed in R-W7.

---

## 3. Workstreams

### R-W1 — Connection Lifecycle and Unit of Work

**Size:** L · **Depends:** nothing · **Closes:** R1 (CRITICAL), R5 (HIGH), R11 ·
**Status: done**

The largest item. Implemented after R-W2 rather than before it (the reverse of this
plan's original sequencing) — R-W2 already closed cleanly on the pre-R-W1 shape, and
doing it that order avoided editing the same five adapter files twice.

#### Why the current design is the way it is

W5.3 recorded an explicit decision, taken by the user at a fork question: single
connection rather than a real `asyncpg.Pool`, chosen because it "matches the SQLite
adapters' existing shape exactly (`main.py`'s F14 unit-of-work reaches into
`job_repo._conn` directly), zero call-site changes, same concurrency ceiling the SQLite
backend already has."

The reasoning was sound but rested on a false premise. The two backends do **not** have
the same concurrency ceiling. `aiosqlite.Connection` dispatches every operation onto a
dedicated background thread through a queue, so concurrent awaits on one connection are
serialized and safe. `asyncpg.Connection` has no such machinery; concurrent awaits raise
`InterfaceError`. The Postgres adapters copied the shape of the SQLite adapters without
inheriting the property that made the shape workable.

This workstream is not overriding the W5.3 decision on preference. It reopens it on
evidence the decision was made without.

#### The fix — scoped smaller than originally planned

The text below (kept for the record) proposed a `conn: Any = None` parameter on *every*
write method across all five ports, so any repository method could theoretically join any
transaction. Implementing R-W2 first surfaced the actual shape of the problem: only two
methods, `JobRepository.create_job` and `OutboxRepository.create_event`, are ever called
together inside a shared transaction anywhere in the codebase — that's the whole of F14's
unit of work. Adding `conn=` to the other ~48 write methods across both backends would
have been speculative generality with no caller motivating it, exactly the kind of
over-engineering this plan's own R-W2.3 argued against for two dead port methods. So R-W1
implements the narrower, traceable version: `transaction()` and `conn=` exist only on the
two methods F14 actually uses.

R1 (concurrency) did not need that narrowing — it applies to every method on all five
Postgres repos, since a bare `asyncpg.Connection` isn't safe for concurrent access no
matter which method is calling it. That's solved once, for free, by making
`PostgresConnection` itself pool-backed instead of narrowing anything: every existing
`self._conn.execute(...)`/`.fetchrow(...)`/`.fetch(...)` call across all five repos now
acquires a connection from the pool for just that call and releases it back, with **zero
call-site changes** in any of the ~48 other methods. Two connection shapes live in
`postgres_conn.py` as a result:

```python
# PostgresConnection — what every repo holds as self._conn for its whole
# lifetime. Each call independently acquires-and-releases from the pool.
class PostgresConnection:
    def __init__(self, pool: asyncpg.Pool): ...
    async def execute(self, query, *args):
        async with self._pool.acquire() as conn:
            return await conn.execute(query, *args)
    # fetchrow, fetch: same shape

# PostgresTransaction — one connection, acquired and held for the life of a
# `job_repo.transaction()` block, with a real transaction already open.
class PostgresTransaction:
    def __init__(self, conn: asyncpg.Connection): ...
    # execute/fetchrow/fetch all run on this SAME connection
```

`main.py`'s unit of work becomes:

```python
async with job_repo.transaction() as tx:
    await job_repo.create_job(job, conn=tx)
    await OutboxRelay.create_outbox_event(
        outbox_repo, aggregate_type="job", aggregate_id=job_id,
        event_type="job.submitted", payload={...}, conn=tx,
    )
```

The context manager owns commit and rollback, so `commit: bool` disappears from the public
surface entirely (replaced by `conn=None` vs. `conn=<transaction>` as the sole signal) and
the `try/except/rollback` block that used to live in `main.py` goes with it.

This shape works because the seam already existed: `SqliteOutboxRepository.create_event`
already accepted `conn=` precisely so a caller could enlist its write in another
repository's transaction. R-W1 promotes that existing parameter from an implementation
detail to the declared port contract, and gives it a real (rather than same-object-by-
convention) meaning on the Postgres side.

#### Tasks

- [x] **R-W1.1 (scoped)** Added `transaction()` and `conn: Any = None` to the
      `JobRepository` port's `create_job`, and `conn: Any = None` to
      `OutboxRepository.create_event` — not to every write method on all five ports (see
      "scoped smaller than originally planned" above). Removed the undeclared `commit`
      parameter both methods' concrete implementations had grown without the port ever
      declaring it (part of finding R5).
- [x] **R-W1.2** `postgres_conn.py` rewritten: `create_pool_with_retry()` replaces
      `connect_with_retry()`, sized from `settings.database.pool_size + max_overflow`
      (closing R11 — those settings are read now). `PostgresConnection` is pool-backed
      (see above); the transaction-holding shape moved to a new `PostgresTransaction` +
      `transaction_scope()`, since a pool-backed facade and a hold-one-connection facade
      are different enough responsibilities to be worth not conflating in one class.
- [x] **R-W1.3** All five Postgres adapters' `initialize()`/`close()` converted to
      pool-based. No other method in `postgres_record_repository.py`,
      `postgres_overlay_repository.py`, or `postgres_observation_repository.py` needed to
      change — they don't participate in F14, and the pool-backed `PostgresConnection`
      makes their existing `self._conn.execute(...)` calls concurrency-safe automatically.
- [x] **R-W1.4** `SqliteJobRepository.transaction()` yields its existing `self._conn`,
      wrapped in try/commit/except-rollback. `create_job`/`create_event` on both SQLite
      adapters now branch on `conn is None` (auto-commit standalone) vs. `conn is
      not None` (join the caller's transaction) instead of the old `commit: bool` flag —
      same behavior, `conn=` is now the single source of truth for it.
- [x] **R-W1.5** `OutboxRelay.create_outbox_event`'s `repo` parameter retyped from
      `SqliteOutboxRepository` to the `OutboxRepository` port — and so was
      `OutboxRelay.__init__`'s `outbox_repo` parameter, which had the same concrete-type
      leak and wasn't called out in the original task list.
- [x] **R-W1.6** `main.py`'s F14 block rewritten against `async with job_repo.transaction()
      as tx:`. `grep -n "_conn" main.py` now matches only two lines of prose comment,
      zero code.
- [x] **R-W1.7** Done in R-W2.5 already (`get_record_count` added to `RecordRepository`).

**Additional, not in the original task list:** `tests/integration/test_postgres_repos.py`'s
F14 regression test (`test_job_outbox_shared_connection_unit_of_work`) called the old
`commit=False`/`job_repo._conn` API directly and would not have compiled against the new
signatures. Rewritten to `async with job_repo.transaction()` with a deliberate `raise`
inside the block — which also makes it a strictly better test than before: it now exercises
the transaction scope's own exception-triggered rollback (asyncpg's `Transaction` context
manager, and the SQLite `transaction()`'s `except Exception: rollback; raise`), not just an
explicit manual `.rollback()` call, matching the real failure mode this exists to catch.
`docs/adr/0001-postgres-backend.md` updated with a dated note — not rewritten — recording
that this implements exactly the follow-up its own "Consequences" section predicted.

**Exit:** R1, R5, R11 closed. `grep -n "_conn" main.py` returns no code references. Full
unit suite 483/483 (unchanged count — no tests added or removed by this workstream, all
existing F14-adjacent tests, including `test_job_submission_rolls_back_on_outbox_write_
failure` in `test_api_smoke.py`, passed against the rewrite without modification).
`test_port_contract_parity.py` 20/20 — confirms `transaction()`'s new port declaration is
implemented identically on both backends. mypy: zero errors in any of the seven files this
workstream touched (a non-`--strict`, transitively-following run surfaced ~60 pre-existing
errors, none in these files — all in unrelated modules like `cache.py`/`observability.py`
that mypy pulled in by import; R-W7.1 is where that untyped surface actually gets decided
on, not here).

**Fallback, not needed:** the plan originally offered guarding the shared connection with
a single `asyncio.Lock` as a lesser mitigation if the full refactor had to be deferred.
Not used — the full fix landed instead.

---

### R-W2 — Complete the Repository Contracts

**Size:** M · **Depends:** R-W1 (same adapter files) · **Closes:** R2 (CRITICAL), R7, R8

R2, R7 and R8 share one root cause: `src/domain/ports.py` is a set of `Protocol` classes,
and Python does not verify at import time that a class claiming to satisfy a Protocol
actually does. Nothing has ever checked. The register of declared-but-unimplemented methods
grew silently to five, one of which (R2) is called in production every day and fails into a
log line nobody reads.

Implementing the methods fixes today's gap. The parity test in R-W2.4 is what stops the gap
reopening, and is the more important half of this workstream.

- [x] **R-W2.1** Implement `purge_expired_jobs(retention_days)` on **both**
      `SqliteJobRepository` and `PostgresJobRepository`: hard-delete jobs whose
      `deleted_at` is older than the cutoff, return the count. This is the live bug — do it
      first. **Done.** Uses each job's own `Job.retention_days` when set (the field already
      existed for this, unused until now), falling back to the parameter otherwise. Deletes
      `job_attempts` rows before the parent `jobs` row on both backends — Postgres enforces
      that foreign key even though SQLite doesn't, so skipping it there only would have made
      the two backends diverge on exactly the query path this fix touches. Batched in chunks
      of 500 to avoid one giant `IN (...)` on a first-run backlog.
- [x] **R-W2.2** Implement `soft_delete_job(job_id)` on both adapters (transition to
      `DELETED`, stamp `deleted_at`, return the updated job). `purge_expired_jobs` is inert
      without it: nothing else sets `deleted_at`, so there is never anything to purge. **Done.**
      Gated on `Job.can_transition_to(JobState.DELETED)` — the domain model's own state
      machine, which had zero callers anywhere before this; only reachable from a terminal
      state (SUCCEEDED/FAILED/CANCELLED/DEAD_LETTERED), matching optimistic concurrency via
      the existing `version` column.
- [x] **R-W2.3** Decide `soft_delete_record` / `purge_expired_records`. They have no caller.
      Following the W6 precedent for aspirational scaffolding, the recommendation is to
      **delete them from the `RecordRepository` port** rather than implement two methods
      nothing invokes. If record-level retention is genuinely wanted, implement both
      adapters and give `JobReaper` a call site — but decide, do not leave them declared and
      absent. **Done — deleted from the port.** Confirmed zero references anywhere else in
      the tree before removing.
- [x] **R-W2.4** Add `tests/test_port_contract_parity.py`. For every Protocol in
      `src/domain/ports.py`, assert each declared method exists on every adapter with a
      compatible signature, via `typing.get_type_hints` and `inspect.signature`.
      Parameterize over both backends. This test must fail today and pass after R-W2.1-3 —
      that is the check that the fix is real. **Done.** Scoped to the five ports `factory.py`
      actually switches between backends for. Two checks per (port, adapter) pair: method
      exists, and every port parameter name is accepted while no adapter-only parameter is
      *required* without the port knowing about it. Confirmed it fails against `HEAD`'s
      committed `job_repository.py`/`postgres_job_repository.py` (0 matches for either method
      name) before the fix landed, and passes now — 20/20.
- [x] **R-W2.5** Resolve R8: either add `job_id` to the port's `create_record` signature, or
      make it a required positional on both adapters. Do not leave a defaulted `job_id=""`
      that silently orphans rows. **Done** — added as a required parameter on the port and
      both adapters. Checked every call site first (`worker_processor.py`, both test files,
      the integration suite): all six already passed `job_id` explicitly by keyword, so this
      was a zero-behavior-change safety fix, not a migration. Also added `get_record_count`
      to the `RecordRepository` port while here (R-W1.7) — `main.py:479` already called it
      through the port-typed variable without it being declared; both adapters already
      implement it with matching signatures.

**Exit:** R2, R7, R8 closed. `test_port_contract_parity.py` green (20/20). A reaper purge
test (R-W7.3) still needed to prove retention deletes rows end to end — R-W2 gives it
something to call, R-W7 is where that test gets written.

---

### R-W3 — Session Lease Correctness

**Size:** S · **Depends:** nothing · **Closes:** R3 (HIGH), R4 (HIGH) · **Status: done**

R3 and R4 are two symptoms of one omission: `lease()` hands out a `Session` without
recording that it is out on loan, and the call sites release it by hand at three separate
points (`worker_scraper.py:347`, `430`, `487`). Manual pairing across three sites in a
275-line method is what produced the double-release; the absence of a checkout marker is
what produced the lost update.

**Implementation note — deviated from the plan as written.** The section below originally
proposed converting `lease()` into an async context manager. That would have meant
rewriting `_process_job`'s exception flow (the try/except spans the whole browser-tier
fetch, including three different failure branches that already do their own job-state and
DLQ bookkeeping) and all 8 existing tests in `tests/test_sessions.py`. A smaller change closes
the same two findings without either: track which session IDs are checked out per domain,
and null the local `browser_session` variable immediately after its first `release()` call
so the crash-path handler — which already correctly checks `if browser_session is not None`
— can no longer fire twice for the same lease. Both root causes close; the public
`lease()`/`release()` signatures are unchanged.

```python
# src/infrastructure/sessions.py — actual shape
class SessionPool:
    def __init__(self, proxy_provider):
        self._sessions: dict[str, list[Session]] = {}
        self._checked_out: dict[str, set[str]] = {}  # domain -> leased session_ids

    def lease(self, domain: str) -> Session:
        # live = not retired AND not already checked out — this is the whole fix for R4
        ...

    async def release(self, domain, session, *, success, blocked) -> Session:
        # scores, replaces the pool's copy, then discards from _checked_out
        ...
```

```python
# worker_scraper.py:428-432 — the whole fix for R3
if browser_session is not None:
    await self.session_pool.release(domain, browser_session, success=..., blocked=is_blocked)
    browser_session = None  # the crash-path release below is now correctly unreachable
                             # for this lease — it already ran, exactly once, above.
```

- [x] **R-W3.1 (revised)** Track checked-out session IDs per domain in `SessionPool`.
      `lease()` excludes them when picking the healthiest live session; if none remain
      available, it mints a new one rather than reusing a session another caller is still
      holding — matching `DomainRateLimiter(default_budget=2)`'s real concurrency ceiling.
      `release()` discards the checkout on every call, including a redundant one (harmless,
      just re-scores from the caller's stale copy — the call-site fix in R-W3.2 is what
      prevents redundant calls from happening at all).
- [x] **R-W3.2 (revised)** `worker_scraper.py:432`: null `browser_session` immediately after
      its release. The existing crash-path comment at the `except Exception` handler
      ("the earlier release() never ran") was false on the double-release path before this
      fix; it's true again now, since the only way to reach that branch with
      `browser_session is not None` is a genuine crash before the first release ran.
      Also gave the Tier-1 lease/release pair (`336-349`) the same treatment implicitly —
      it was already single-release-safe (`try_tier1` never raises), but now benefits from
      the same checked-out tracking against concurrent jobs.
- [x] **R-W3.3** Added `test_concurrent_leases_on_same_domain_get_different_sessions` to
      `tests/test_sessions.py`. Confirmed against a reconstructed copy of the pre-fix
      `lease()` (this file is new/untracked, no prior git revision to diff against) that two
      sequential leases without an intervening release returned the *same* `session_id`
      before the fix — the bug was real, not hypothetical. Also had to update two
      *existing* tests (`test_session_pool_reuses_same_session_across_leases_until_retired`,
      `test_persona_proxy_binding_survives_lease_and_release_cycle`) that called `lease()`
      twice in a row with no `release()` between — a pattern that only "worked" because of
      the bug this workstream fixes. Both now insert the release the real call sites always
      do. 10/10 passing.
- [x] **R-W3.4** Done as specified. `worker_scraper.py` now checks
      `job.use_proxy and should_attempt_http_tier(throttle_profile)` before leasing for
      Tier 1, using the exact predicate `try_tier1` itself gates on — `throttle_profile` was
      already loaded at that point in the method.
- [x] **R-W3.5** Done, as a periodic sweep rather than a hard cap: every 50th `release()`
      call (across all domains), drop any domain bucket whose sessions have all retired.
      Simpler than a TTL and needs no wall-clock dependency; a bucket only grows back if the
      domain is actually scraped again, at which point `lease()` mints a fresh session for
      it regardless.

**Exit:** R3, R4 closed. `tests/test_sessions.py` 10/10, full unit suite 483/483
(`--ignore=tests/integration`, the Postgres integration suite needs a live server this
session doesn't have). Two incidental fixes landed alongside, outside this plan's finding
register: `worker_scraper.py`'s import block was unsorted and carried three dead imports
(`timezone`, `Job`, `setup_production_logging`) — this closes R-W8.3 early, since the
import block needed touching anyway for `should_attempt_http_tier`. Two local test stubs
in `tests/test_resilience_turbo_guard.py` (`_BrowserFallbackStub`, `_PromotingEngine`) had
never been updated when `ScraperEngine.start()` gained a `proxy` parameter in the earlier
uncommitted P3 work — both `TypeError`'d on collection before this fix; not a finding this
plan tracked, discovered by running the full suite before starting R-W3.

---

### R-W4 — Proxy Credentials as a Domain Value

**Size:** S · **Depends:** nothing · **Closes:** R6 (HIGH) · **Status: done**

`ProxyProviderPort.next_proxy()` returns `str | None`, and `provider.py` documents that
string as `http://user:pass@ip:port`. Two consumers then interpret it differently: curl_cffi
parses embedded userinfo natively and authenticates; Playwright takes `{"server": <string>}`
and expects `username`/`password` as sibling keys, so the credentials are never applied and
the proxy answers 407.

**Implementation note — deviated from the plan as written.** The section below originally
proposed a `Proxy` domain value object threaded through `ProxyProviderPort`, changing its
return type and rippling into `Session.proxy`, `FetchRequest.proxy`, and `http_fetcher.py`.
On inspection, Tier 1 (`http_fetcher.py:44`) already passes `request.proxy` straight to
curl_cffi's own `proxy=` kwarg, which parses embedded userinfo natively — that path was
never broken and needed no change. The actual defect is entirely local to the Playwright
call site: `worker_scraper.py` built `{"server": browser_session.proxy}` from the raw
string. A single pure function at the point of consumption — `parse_proxy_url` in
`browser/pool.py`, the only module that builds Playwright's proxy dict — closes R6 without
touching `ProxyProviderPort`, `Session`, `FetchRequest`, or any of their existing callers
and tests.

- [x] **R-W4.1 (revised)** Added `parse_proxy_url(proxy: str) -> dict[str, str]` to
      `src/infrastructure/browser/pool.py` instead of a new domain type — splits
      `scheme://[user:pass@]host[:port]` into Playwright's `{"server", "username"?,
      "password"?}` shape via `urllib.parse.urlsplit`.
- [x] **R-W4.2 (revised, scope reduced)** No change to `ProxyProviderPort` or
      `StaticProxyProvider` — Tier 1 already consumes their `str | None` output correctly,
      so widening the port's return type would have been surgery with no corresponding bug
      to fix.
- [x] **R-W4.3** `worker_scraper.py`'s browser-tier proxy construction now calls
      `parse_proxy_url(browser_session.proxy)`. `http_fetcher.py` needed no change — it was
      already correct (see implementation note above), now covered by a regression test
      that locks that in.
- [x] **R-W4.4** `FetchRequest.proxy`'s docstring corrected: it previously claimed
      `'http://host:port'` (no credentials) when the real value, from `SessionPool`, is
      `'http://user:pass@ip:port'`. Now states that explicitly and cross-references
      `parse_proxy_url` for why Tier 2 needs the split.
- [x] **R-W4.5** New `tests/test_browser_proxy.py`: 5 cases on `parse_proxy_url` (basic
      split, no-credentials, `socks5://`, no-port, percent-encoded credentials) plus one
      Tier-1 pass-through test against `ImpersonatingHttpFetcher` with a monkeypatched
      `AsyncSession`, asserting curl_cffi's `proxy=` kwarg receives the credential-bearing
      string byte-for-byte unchanged. **Caught a real second bug while writing it**:
      `urlsplit().username`/`.password` do not decode percent-escapes (that's `unquote`'s
      job, not `urlsplit`'s) — a password containing `%40`/`%3A` etc. would have reached
      Playwright still URL-encoded instead of as the literal credential. Fixed in
      `parse_proxy_url` itself (added `unquote()`), not by weakening the test. 6/6 passing.

**Exit:** R6 closed. One proxy string produces authenticated requests on both tiers.
6 new tests, full unit suite 489/489.

---

### R-W5 — Postgres Schema Types

**Size:** M · **Depends:** R-W1 · **Closes:** R9 · **Status: done**

Every Postgres table stores timestamps as `TEXT` and booleans as `INTEGER`. SQLite has no
native type for either, so its adapter had no choice; the Postgres adapter inherited the
workaround along with the schema it was translated from. The cost is real: no range index on
any timestamp column, no `BETWEEN` without a text-comparison hack, and a format mismatch
where `now()::text` writes `2024-01-01 12:00:00.123456+00` while application code writes
`2024-01-01T12:00:00`, so the two sort inconsistently in the same column.

Sequenced after R-W1, as planned — changing column types while also changing how
connections are acquired would have made a failure in either harder to attribute.

- [x] **R-W5.1** Convert timestamp columns to `timestamptz` and flag columns to `boolean`
      across all five Postgres adapters' DDL. **Done.** `blocked`/`success` on
      `strategy_observations` (`postgres_observation_repository.py`) were the only boolean
      columns anywhere in the schema; every genuine timestamp column across all five files
      (`jobs`, `job_attempts`, `records`, `outbox_events`, `extraction_schemas`,
      `extraction_overlays`, `strategy_observations`, `feedback_items`,
      `evaluation_results`, `domain_profiles`) converted to `TIMESTAMPTZ`. Enum/status-like
      `TEXT` columns (`state`, `status`, `change_type`, `strategy`, etc.) were left alone —
      they were never the finding.
- [x] **R-W5.2** Remove the `now()::text` call in `postgres_record_repository.py:31` in
      favour of `now()`. **Done**, same edit as the DDL conversion above.
- [x] **R-W5.3** Adjust the row-mapping helpers, which currently parse ISO strings and will
      receive `datetime` and `bool` objects instead. **Done** — every
      `datetime.fromisoformat(row[...])` and `bool(row[...])` in all five `_row_to_*`
      helpers replaced with a bare `row[...]`, since asyncpg returns native `datetime`/
      `bool` for `timestamptz`/`boolean` columns directly. Every INSERT/UPDATE call site's
      `.isoformat()`/`int(...)` on the write side removed the same way — asyncpg accepts
      Python `datetime`/`bool` objects natively as query parameters.
      **Two call sites needed care, not just deletion**: `JobRepository.update_attempt`'s
      `finished_at` and `RecordRepository.update_record`'s `last_seen` are declared on the
      *port* as `str` (an ISO string) — that's the SQLite-side convention, unchanged by
      this workstream. The Postgres side now does `datetime.fromisoformat(finished_at)` /
      `datetime.fromisoformat(last_seen)` before binding, since asyncpg needs a native
      `datetime` for a `TIMESTAMPTZ` column and won't accept a raw string the way SQLite's
      `TEXT` column would.
- [x] **R-W5.4** Extend `tests/integration/test_postgres_repos.py` to assert round-trip
      fidelity of a timezone-aware datetime — the regression this change is most likely to
      introduce. **Done** — `test_timestamp_round_trips_as_timezone_aware`, deliberately
      using a non-UTC offset (IST, +5:30) rather than UTC: a UTC round-trip wouldn't catch
      a driver silently dropping `tzinfo`, since UTC's offset is zero either way. Asserts
      both that `created_at.tzinfo is not None` after `create_job`+`get_job`, and that the
      exact instant survives (`fetched.created_at == original`), plus that `heartbeat()`'s
      write path produces a tz-aware `last_heartbeat_at` too. Skip-gated on
      `TEST_POSTGRES_URL` like the rest of the file — not run this session (no live
      Postgres available), but confirmed to parse, lint clean, and use only fields/methods
      that exist.

**Exit:** R9 closed. No `TEXT` timestamp or `INTEGER` boolean remains in any Postgres DDL
block. `grep -n "isoformat" src/infrastructure/repositories/postgres_*.py` matches only the
two deliberate string-to-datetime conversions on the two port-declared `str` parameters
above — everywhere else, timestamps flow as native `datetime` objects end to end.
`test_port_contract_parity.py` still 20/20 (this workstream didn't touch any port
signature). Full mechanical verification (a real round-trip against a live server) is
blocked on the same thing every Postgres-adapter test in this plan has been blocked on:
no live Postgres available this session, and CI itself blocked on GitHub Actions billing.

**Interaction with W5.5:** the deferred migration-tooling rewrite must target this,
post-R-W5, schema — the `TEXT`/`INTEGER` shape it would otherwise have been written
against no longer exists.

---

### R-W6 — Configuration and Operational Hardening

**Size:** S · **Depends:** R-W1 (for R-W6.2's pool sizing only — see below) · **Closes:**
R10, R12, R13, R14 · **Status: done**

Independent MEDIUM items, grouped because they are all small and all touch the persistence
layer's operational edges. Done ahead of R-W1 (which is still not started) — R-W6.1, .3, .4
have no dependency on it at all, and R-W6.2 was adapted to apply to the current single
connection rather than a pool that doesn't exist yet.

- [x] **R-W6.1** (R10) Fail fast: if `PERSISTENCE_BACKEND=postgres` and `DB_URL` is not
      explicitly set, raise at startup rather than connecting to `postgres:postgres@localhost`
      by default. `factory.py:16` is the natural place. **Done** — a `_postgres_dsn()` helper
      in `factory.py` checks `os.environ.get("DB_URL")` directly (not
      `settings.database.url`, which always has that default baked in and so can't
      distinguish "set to the default" from "never set") and raises `RuntimeError` before
      any of the five `make_*_repository()` functions construct an adapter.
- [x] **R-W6.2** (R12) Add `timeout` and `command_timeout` to pool creation, plus a bounded
      startup retry with backoff. Containers routinely are not accepting connections when the
      app first tries. **Done, scope adapted**: there is no pool yet (R-W1 not started), so
      this applies to the single connection instead — a new `connect_with_retry()` in
      `postgres_conn.py` wraps `asyncpg.connect()` with a 10s connect timeout, a 30s
      per-query `command_timeout`, and up to 5 attempts with doubling backoff (0.5s, 1s,
      2s, 4s). All five Postgres adapters' `initialize()` now go through it instead of a
      bare `asyncpg.connect(self.dsn)`. When R-W1 lands, `connect_with_retry`'s
      timeout/backoff values are exactly what `create_pool()` should reuse — this doesn't
      need re-deciding then, just re-plumbing into pool creation instead of a single
      connect.
- [x] **R-W6.3** (R13) Replace `get_or_create_profile`'s check-then-insert with
      `INSERT ... ON CONFLICT (domain) DO NOTHING` followed by a fetch, on both backends.
      **Done** on both `observation_repository.py` and `postgres_observation_repository.py`.
      The Postgres version uses `RETURNING *` to fetch the row in the same round-trip when
      it inserts (matching the `RETURNING *` pattern `postgres_job_repository.py` already
      uses for `update_job_state`), falling back to a plain `SELECT` only on the conflict
      path.
- [x] **R-W6.4** (R14) Add `limit`/`offset` to `list_schemas()` and `list_overlays()`,
      matching `list_jobs`/`list_records`. **Done** — added to the `OverlayRepository` port
      and both adapters, both defaulting to `limit=50, offset=0`. Checked callers first:
      `list_schemas()` has none anywhere in the tree; `list_overlays()` has two, both in
      `tests/test_extraction_schema.py`, both called with no `limit`/`offset` arguments —
      the added parameters are additive defaults, zero behavior change for existing
      callers. `tests/test_port_contract_parity.py` (R-W2.4) caught nothing here because
      the port and both adapters were updated together in one pass — exactly the case that
      test exists to guard on the next divergence.

**Exit:** R10, R12, R13, R14 closed. Full unit suite 489/489;
`tests/test_port_contract_parity.py` and `tests/test_extraction_schema.py` both green
after the `OverlayRepository` signature change.

---

### R-W7 — Close the Verification Gaps

**Size:** M · **Depends:** R-W1, R-W2, R-W3 · **Closes:** R16 · **Status: done**

Every finding in this plan was invisible to CI. That is the finding behind the findings.
`mypy --strict` runs on `src/domain` only, which is why six Protocol violations in `main.py`
never failed a build. The integration suite exercises the Postgres adapters sequentially,
which is why a concurrency bug that fires on the second simultaneous query never surfaced.
Nothing calls `JobReaper.purge_once()` in a test, which is why a method missing from both
adapters went unnoticed.

- [x] **R-W7.1 (scope adjusted)** Widen the mypy CI job past `src/domain`. **Done for
      `src/infrastructure/repositories/`; `main.py` and `src/bootstrap.py` deferred.**
      The real backlog was checked before committing to a scope: `mypy --strict` on
      `main.py` + `src/bootstrap.py` + `src/infrastructure/repositories/` together found
      **219 errors across 33 files** — too large to fix as a side effect of this task
      without it consuming far more of this session than the rest of the plan combined.
      Scoping to just `src/infrastructure/repositories/` (plus `src/domain`, already
      clean) found 74 errors, all mechanical (`no-untyped-def`, missing generic type
      parameters) except one — fixed all of them, confirmed `mypy` now reports
      **"Success: no issues found in 25 source files"**, and widened
      `pyproject.toml`'s `[tool.mypy] files` for real. `main.py`/`bootstrap.py`'s ~145
      remaining errors are recorded here as a known, un-closed gap rather than silently
      dropped — a natural next step, not part of this plan.
      **Two fixes were not mechanical, worth flagging on their own:**
      - `pyproject.toml` was missing `plugins = ["pydantic.mypy"]`. Without it, mypy
        doesn't know `Field(default=...)` makes a constructor argument optional, and
        flagged `DomainProfile(domain=domain)` (omitting the defaulted
        `preferred_strategy`/`overlay_id`) as a missing-argument error — a false
        positive, not a bug. This would have produced the same false positive against
        *any* pydantic model built with an omitted default anywhere in the newly-covered
        scope, so it's a real, general fix, not a one-off suppression.
      - `asyncpg` ships no type stubs, so mypy refused to analyze it under `--strict`.
        Added a `[[tool.mypy.overrides]]` for `asyncpg.*` with `ignore_missing_imports =
        true` — every `asyncpg.Pool`/`Connection`/`Record` attribute already resolves to
        `Any` regardless (no stubs exist to be more precise against), so this loses no
        real checking.
      `.github/workflows/ci.yml`'s `typecheck` job renamed to name the actual scope.
- [x] **R-W7.2 (revised)** Add a concurrency test. **Done, scope corrected**: the plan's
      text said "across repos sharing the pool," but each repo class constructs its own
      pool independently — nothing in this codebase needs one pool shared across all
      five. The meaningful test is many concurrent calls against *one* repo instance
      (that's what R1 was actually about — one process-wide `AppContainer` serving every
      request plus three background tasks through the same repo objects), run across two
      different repos in one `asyncio.gather` to also confirm no cross-repo interference.
      `test_concurrent_queries_do_not_raise_interface_error` in
      `tests/integration/test_postgres_repos.py`, skip-gated like the rest of that file
      — not run this session (no live Postgres), but the mechanism is exact: pre-R-W1,
      the same connection object handling two concurrent calls raises
      `asyncpg.exceptions.InterfaceError` deterministically, not intermittently, so this
      is not a flaky-by-nature test.
- [x] **R-W7.3** Test `JobReaper.purge_once()` end to end: soft-delete a job, advance the
      clock past retention, assert the row is gone. Parameterized over both backends.
      **Done on both.** SQLite: `test_purge_once_actually_deletes_rows_end_to_end` in
      `tests/test_reaper.py`, run for real against a real `SqliteJobRepository(db_path=
      ":memory:")` — **4/4 passing**, confirmed distinct from the file's existing
      `test_purge_once_delegates_retention_days`, which only proves `JobReaper` calls
      `purge_expired_jobs` correctly against a `FakeJobRepo`, not that the real
      implementation deletes anything (exactly the gap that let R2 go unnoticed while
      `purge_expired_jobs` was undeclared on the port). No clock-mocking needed:
      `retention_days=0` on the job makes it immediately eligible, since any
      non-negative elapsed time satisfies `>= timedelta(days=0)`. Postgres: the same
      pattern in `test_postgres_repos.py`, skip-gated, not run this session.
- [x] **R-W7.4** (R16) Add `tests/test_proxy_provider.py` covering round-robin wraparound,
      empty list returning `None`, and `SCRAPER_PROXY_LIST` parsing including trailing commas
      and stray whitespace. **Done — 12/12 passing**, covering exactly the three cases
      named plus percent-adjacent edge cases (single entry, explicit list overriding the
      env var).

**Exit:** R16 closed. R1 has a test that would fail without its fix (mechanism verified,
not run — no live Postgres). R2 has a test that *did* fail without its fix and now passes
(run for real, SQLite side). R4 — see R-W3's own concurrency test
(`test_concurrent_leases_on_same_domain_get_different_sessions`), already closed there.
`mypy --strict` genuinely widened and passing (25 files, 0 errors) — not just claimed.
Full unit suite: **496/496**, up from 483 at the start of this session's work
(20 parity + 6 proxy-tier + 12 proxy-provider + 1 reaper-end-to-end + concurrent-lease
regression, net of the 5 deleted `ExplorationPolicy` tests in R-W8).

**Blocked on billing.** None of the Postgres-specific tests (R-W7.2's concurrency test,
R-W7.3's Postgres-side purge test, R-W5.4's timezone round-trip, R-W2's F14 rollback
test) can be verified in CI while GitHub Actions is blocked at the account level (run
`32557236953`, all seven checks failing before any job starts). Run `postgres-repos`
locally against a real `postgres:16-alpine` and record the result in the commit message.
Do not treat local green (SQLite-side tests, mypy, ruff — all genuinely run this session)
as equivalent to CI green for the Postgres-side tests, which were written and reasoned
through carefully but not executed.

---

### R-W8 — Dead Code and Drift

**Size:** S · **Depends:** nothing · **Closes:** R15 · **Status: done**

- [x] **R-W8.1** (R15) Decide `ExplorationPolicy`. It has no caller in `src/`, and
      `record_outcome` is a `pass` stub. Either wire it into `StrategySelector` / the
      fetch-tier decision so its Beta-sampling fix takes effect, or delete it under the W6
      aspirational-scaffolding precedent. The uncommitted diff currently fixes a real
      sampling bug in code that never runs. **Decided: deleted.** Checked what "wire it in"
      would actually mean first — `StrategySelector` is not a slightly-different version of
      the same idea, it's a different mechanism entirely: a periodic background task
      (hourly) that evaluates historical observations and writes the winner into
      `DomainProfile.preferred_strategy`, already live and already what `adaptive_fetch.py`
      reads. `ExplorationPolicy` was designed for live per-request Thompson-sampling
      exploration, a genuinely different decision (explore vs. exploit *per fetch*, not
      *per hour*) that nothing ever called into being. Wiring it in for real would mean
      implementing `record_outcome` (currently `pass`, claims to update Valkey/DB and
      doesn't), and deciding how per-request exploration coexists with
      `StrategySelector`'s periodic override of the same field — real design work with no
      existing caller motivating it. Deleted `src/application/exploration_policy.py` and
      its dedicated test class (`TestExplorationPolicy` in
      `tests/test_increment_modules.py`, which also covers `StrategyEvaluator`/
      `SLOMonitor`/`AutoRollback` — only the one class and its now-unused import came out).
      Confirmed zero remaining references anywhere in the tree after deletion.
- [x] **R-W8.2** `migrate_sqlite_to_postgres.py` still defaults to `spacescraper_intel.db`;
      W5.1 renamed the live default to `spacescraper_jobs.db`. A default-args run now reads a
      stale file and produces a confusing partial migration instead of a clear failure. Folds
      into the W5.5 rewrite; fix the default now regardless, since it is a one-line trap.
      **Done** — both defaults (the `MigrationTool.__init__` parameter and the `--sqlite-path`
      CLI flag) updated. Nothing else in this file touched: it has 25 pre-existing,
      unrelated ruff findings (deprecated `typing.List`/`Dict`/`Optional`, an f-string with
      no placeholders, unsorted imports) that belong to the W5.5 rewrite this file is
      already earmarked for, not this one-line trap fix.
- [x] **R-W8.3** Sort the `worker_scraper.py` import block (`ruff check --fix`, I001) and drop
      the unused `timezone`, `Job`, `setup_production_logging` imports. **Done in R-W3** —
      the import block needed touching anyway to add `should_attempt_http_tier`, so this
      landed there rather than being deferred to this pass.
- [x] **R-W8.4** `sqlite_tracker.py:150-153` has a bare `except Exception: pass` in `close()`,
      inconsistent with every other handler in this diff. Add `logger.debug(..., exc_info=True)`.
      **Done.**

**Exit:** R15 closed. No dead module carrying an unreachable bug fix.

---

## 4. Sequencing

```
R-W1 (connection + UoW)  ─┬─→ R-W2 (contracts) ─┬─→ R-W7 (verification)
                          ├─→ R-W5 (schema)     │
                          └─→ R-W6 (ops)        │
                                                │
R-W3 (session lease) ───────────────────────────┤
R-W4 (proxy value) ─────────────────────────────┘

R-W8 (dead code) — independent, any time
```

Order and why:

1. **R-W1 first.** R-W2, R-W5 and R-W6 all edit the same five adapter files. Doing them
   before the connection refactor means editing each file twice and resolving the overlap by
   hand.
2. **R-W2 second**, and within it R-W2.1 before anything else in this plan that is not R-W1.
   Retention purge is broken in production right now.
3. **R-W3 and R-W4 in parallel** with the above. They touch `worker_scraper.py` and the proxy
   path, sharing no files with the persistence work.
4. **R-W7 last**, because it verifies the rest. R-W7.1 (mypy scope) can land early and
   independently — it will immediately surface R5 as a build failure, which is useful pressure
   rather than noise.
5. **R-W8 whenever.** No dependencies either way.

Commit granularity: one commit per workstream, or per task where a task stands alone. R-W2.1
in particular deserves its own commit, since it is a production bug fix and should be
revertable without unwinding a refactor.

---

## 5. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| R-W1 breaks the F14 unit-of-work guarantee while refactoring it | Medium | High — silent data loss between job and outbox | `test_job_outbox_shared_connection_unit_of_work` already exists (`test_postgres_repos.py:195-211`). Run it before and after. Do not modify that test as part of R-W1 |
| R-W1's port changes ripple further than expected | Medium | Medium | Landing R-W7.1 (widen mypy) first turns ripples into compile errors rather than runtime surprises |
| R-W5's type change breaks row mapping subtly (naive vs aware datetimes) | Medium | Medium | R-W5.4 round-trip test. The codebase already has a naive/aware inconsistency; avoid reintroducing it |
| Nothing here can be verified in CI while Actions billing is blocked | Certain | High | Run the suites locally against real Postgres; record results explicitly; do not report anything as verified that was not run |
| The uncommitted tree diverges further from `master` while this work proceeds | High | High | The merge is already conflict-heavy. Every workstream here that touches `main.py`, `ports.py` or the repositories widens the conflict. Consider landing R-W2.1 and R-W3 as small commits first, then deciding merge-versus-continue |
| R-W2.3 deletes port methods someone intended to build on | Low | Low | They have no callers and no implementations. If the intent existed it was never expressed in code; an ADR is the place to record the decision either way |

---

## 6. Explicitly Out of Scope

Named rather than silently omitted:

- **The `master` merge.** Sixteen conflicted files, four security regressions, two
  modify/delete conflicts. Separate work, separately analysed.
- **W5.5 migration tooling.** Already deferred by the architecture remediation plan; R-W5
  changes the target schema it must be written against, so it should follow R-W5, not precede
  it.
- **W7 and W8** of the architecture remediation plan (observability/scale, documentation
  consolidation). Untouched by this review.
- **`database_models.py`.** Stale `Opportunity`-era SQLAlchemy ORM, unused and dead. Flagged
  in W5.3 as a separate cleanup; still is.
- **A durable `SessionRepository`.** `sessions.py` documents in-memory/per-process as a
  deliberate scope choice with a stated upgrade path. R-W3 fixes correctness within that
  scope; it does not change the scope.

---

## 7. Relationship to the master merge

`master` is 21 commits ahead with a Deep Research feature built on files this branch deleted.
A trial merge produced 16 conflicted files and 42 conflict hunks.

The interaction that matters for sequencing: **`src/domain/ports.py`,
`src/infrastructure/repositories/observation_repository.py` and `main.py` are conflicted in
that merge and are edited by R-W1, R-W2 and R-W6 here.** Every workstream in this plan that
touches them makes that merge harder.

Two coherent orders exist:

- **Fix first, merge second.** Land this plan, then merge. The merge gets harder, but the code
  being merged is correct. Preferable if the Deep Research work is not urgent.
- **Merge first, fix second.** Resolve the merge, then apply this plan to the merged tree.
  Avoids compounding the conflict, but means resolving conflicts in code with two known
  CRITICAL defects in it. The merge audit's own findings (a fail-open fan-out cap, a missing
  admin gate) would be resolved alongside these.

The one item that should land immediately either way is **R-W2.1**. Retention purge is broken
in production today, the fix is small and self-contained, and it conflicts with nothing.
