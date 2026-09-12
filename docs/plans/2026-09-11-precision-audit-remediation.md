# Precision Audit Remediation Plan

**Date:** 2026-09-11
**Source:** Precision Defect Auditor V4.1 sweep (5 hunter clusters, 40 findings) + 1 external review finding (Codex, PR #9 on commit `0ee233ac7b`) + 1 sibling defect found while verifying it.
**Total work items:** 42
**Status:** in progress.

| Item | State | Branch |
|---|---|---|
| D32 | **applied** | merged `d79de08` |
| R1, R2 | **applied** | merged `d79de08` |
| D1, D2, D22, D30, D31 | **applied** | `fix/audit-p0-trust-boundaries` |
| D14 | **applied** — event-loop blocking in P0; URL policy unified and the duplicate DNS lookup removed in P6. The two transports are deliberately *not* merged, see below | `fix/audit-p0-trust-boundaries`, `fix/audit-p5-browser-pool-resources` |
| **R3** (new) | **applied** — see below | `fix/audit-p2-maps-extraction` |
| D5, D12 | **applied** | `fix/audit-p2-maps-extraction` |
| D6 | **applied** — made diagnosable in P2, then deleted outright, see below | `fix/audit-p2-maps-extraction`, `fix/audit-p5-browser-pool-resources` |
| D3, D15, D16, D25, D26 | **applied** | `fix/audit-p3-migration-schema` |
| D4, D13, D18, D19, D20, D21, D9 | **applied** | `fix/audit-p4-queue-worker-reliability` |
| D10, D11, D28 | **applied** | `fix/audit-p5-browser-pool-resources` |
| D7, D17, D23, D24, D27, D29, D33, D34, D37, D38 | **applied** | `fix/audit-p5-browser-pool-resources` |
| all others | proposed, not applied | — |

**D4 was decided in favour of deleting the third queue implementation.** The plan asked for an explicit decision. `RedisQueueWorker` had exactly one importer (`worker_discovery.py`) and, once discovery moved onto `stream_queue`, none — so the module is deleted rather than left as a fourth way to move a message. Two comments that named it as a live credential-logging site were corrected; two that describe it as pre-migration history were left, because they are.

**D13 was two defects, and the second one hid the first.** The XCLAIM reply-shape bug is what the audit found. But `_process_entry` also *silently acked and discarded* anything that failed to parse — so even after the shape fix, an unparseable pending entry would have vanished without a trace instead of spinning. `ValidationError` joins the caught tuple and the raw entry is now written to the DLQ before the ack. `push_dlq` needs a `QueueMessage` to serialise and by definition there isn't one, hence `_dlq_raw`.

**D18's `return_exceptions=True` was already present** — the audit's diff shape implied adding it. The defect was purely that the results were discarded. Total failure now raises; partial failure still acks, since retrying the whole message would re-deliver to the channels that already succeeded.

**D19's partial-failure case is the one worth pinning.** Marking URLs seen *after* the loop instead of before would still lose the children of a mid-loop raise. Each URL is marked after its own push lands, and fan-out-capped URLs are marked too — dead-lettering is terminal, so they are done being considered.

**D20 used the lock, not a per-transaction connection.** Both were offered. A separate connection would have changed the connection lifecycle every repository method depends on, which is the exact risk the plan flagged this item for; an `asyncio.Lock` held by `transaction()` and acquired by the eight methods that commit changes nothing about which connection anything runs on. The cost is a re-entrancy rule — inside a `transaction()` block, use the yielded connection, not another write method on the same repo — which `create_job` already required via `conn=` and which is now documented in the docstring. Landed on its own commit, per the plan.

**D9's fix shape was conditional, and the condition held.** The audit said to initialise in the shared worker entry path, "if no shared entry path exists, that absence is the actual finding". There was none — four workers, four separate `asyncio.run(worker.run())` calls. `src/infrastructure/worker_runtime.py` is that path; `worker_scraper`'s own `initialize()` call is removed as redundant, since leaving it would build a second Valkey client and orphan the first. The invariant test globs `worker_*.py` rather than listing them, so a fifth worker is covered on the day it lands.

**P4 verification scope.** 1021 tests pass (995 before, +26). `mypy --strict` over its configured scope: clean. import-linter: exit 0. ruff: **88 findings repo-wide in both the working tree and a clean HEAD worktree** — parity, no regression. bandit **over the nine changed source files only**: 0 medium-or-above before and after, **+2 LOW B101** (`assert self._pool is not None`, `assert self._valkey is not None`), joining 35 pre-existing instances of the same idiom in those files. No Postgres and no live Valkey were available, so D21 is asserted against a fake pool that reproduces `PostgresConnection`'s per-statement acquisition, and the queue tests run on fakeredis — stated in each test's docstring rather than implied to be server-level verification.

**D3's planned diff named a field that does not exist.** It incremented `self.stats.errors`, but `self.stats` is a `List[MigrationStats]`, not a single record — the diff would not have run. The applied fix drops that line: on the raise path the report is never generated anyway, so the counter had no reader.

**D3's sweep found one more instance, in a different shape than the audit described.** The audit looked for count-before-write, and `_upsert_opportunities_batch` was the only place with it. But `_migrate_runs` credited `updated += 1` whenever `ON CONFLICT DO NOTHING` matched an existing row — reporting an update for a statement that wrote nothing. Same property violated ("reported counts reflect rows actually written"), different mechanism. `MigrationStats` gained a `skipped` field so a no-op reads as a no-op.

**The aborted-transaction `raise` was applied to three loops, not one.** `_migrate_runs` and `_migrate_dead_letters` had the same log-and-continue handler as the opportunities batch, and the same reason it is wrong: Postgres refuses every subsequent statement on a connection whose transaction failed, so continuing generates one further error per remaining row and then fails at `commit()` regardless. `_migrate_domain_profiles` was deliberately **left** continuing — it writes through `PostgresObservationRepository`, whose `PostgresConnection` acquires a fresh pooled connection per statement, so one bad row there genuinely does not poison the next.

**D25 keys off the source row's own primary key when it has one.** The composite fallback (`job_id|url|created_at|error_message`) deliberately omits `retry_count` and `status`: those change between runs, and keying on them would reintroduce the duplication the fix exists to remove.

**D26's `--verify` was implemented rather than removed.** `verify_migration.py` already contained the checks and exposed `verify_migration()`/`print_results()`; nothing called it. The flag now runs them and propagates a non-zero exit, and says so explicitly when skipped under `--dry-run` rather than appearing to have run.

**D16's real fix is the parity test, as the plan predicted — and it is wider than the two columns.** `create_observation` used `INSERT INTO strategy_observations VALUES (...)` with no column list, so a column added to the table would have been filled with its default instead of raising. That is the mechanism by which the two fields went missing without anything failing. Columns are now named. `tests/test_backend_schema_parity.py` checks both backends' DDL against `StrategyObservation` and `DomainProfile`, so the next added field cannot reach one backend and skip the other.

**D15 confirmed asymmetric, and SQLite was right by accident.** Executed: SQLite accepts `SET last_seen = ?, last_seen = ?` and applies the *last* assignment, which is the intended semantics; Postgres rejects the same statement with 42601. Both adapters now build the SET clause from a dict keyed by column, which preserves SQLite's observable behaviour exactly while making the statement legal on Postgres.

**D5's planned diff was incomplete and would not have worked.** Widening the content-type gate alone still leaves `json.loads(body)` at `engine.py:152` choking on the `)]}'` prefix, so a newly-admitted Maps payload would have been dropped by the `except` as an interception error — one silent failure traded for another. The applied fix strips the XSSI guard before parsing, and does so for `application/json` bodies too, since those carried the guard and were already being discarded as parse errors. A JS-typed body with no guard is still skipped, so script bundles are not buffered.

**D6 deliberately not repaired, per §13.** Pinning the real search-payload shape inside `APP_INITIALIZATION_STATE` needs a live capture, and the consent wall makes that expensive; guessing at it is exactly the unverified edit §13 exists to prevent. The fallback now logs a warning naming the byte count when it finds embedded JSON but matches no business arrays, turning a silent "no businesses here" into a diagnosable parse failure. **Still open:** either walk into the real payload once a capture exists, or delete the fallback.

**D12 was narrower than the audit implied.** Of seven malformed payload shapes tested, only `[[None]]` actually raised — `container = container[0]` at line 110 unwraps to `None` and `len()` on line 113 then raises `TypeError`. The other six were already handled by the existing `isinstance` guards. The finding was real at the exact line reported; its blast radius was one shape, not the class.

**D14 scope split, decided during implementation.** The finding bundled two things. The security half was already closed by D1: both transports import the same `is_private_ip`, so repairing the classifier repaired both boundaries at once. Of what remained:

- **Applied** — `GuardedTransport.handle_async_request` called `validate_outbound_url` and `resolve_and_validate_hostname` inline, both of which use blocking `socket.getaddrinfo`, stalling the event loop for every DNS lookup on that client. Its sibling `SSRFValidatingTransport` uses `loop.getaddrinfo` and never had this. Now offloaded with `asyncio.to_thread`, which keeps the fail-closed semantics byte-identical rather than rewriting one to match the other.
- **Deferred** — collapsing the two transports into one. `SSRFValidatingTransport` lacks `allowed_private_hosts` (which `create_scoped_client` depends on) and `GuardedTransport` lacks the `SSRF_EGRESS_ENFORCE` log-only mode; they also differ on `Host` header handling (`setdefault` vs overwrite). Eleven tests in `tests/test_security_ssrf_transport.py` construct `GuardedTransport` directly. `http_client.py:89-104` already documents this as "a refactor with its own test surface" — that assessment is correct, and doing it under a security-fix branch would mix a behavioural refactor into changes that need to be reviewable line by line.

**D31 closed by deletion rather than by fixing the latch.** No caller anywhere passes `allow_private=True` to `HttpClient.get_client`; the sole production call site (`url_policy.py:161`) passed `False` explicitly, and every test builds `GuardedTransport` directly. A process-wide switch that disables the SSRF guard, used by nobody, is worth removing rather than making per-caller. The parameter is gone; `create_scoped_client` remains the supported way to reach one private endpoint.

**D30 reproduced before fixing, per §13.** Result: no shipped configuration triggers it — `allowed_domains`/`denied_domains` both default empty (`config_settings.py:178-179`) and every in-repo pattern uses the `*.suffix` form. It is operator-triggered, not live. Fixed anyway, because the docstring documents these as "glob patterns" while only `*.` was implemented: `evil*.com`, `*evil.com` and a bare `*` all fell through the matcher and matched nothing, so a denylist entry silently permitted exactly what it was written to block. Now routed through `fnmatch`, with the `*.suffix` apex-plus-subdomain case kept as a special case because plain glob would miss the apex.

**D1 needed one correction to the planned diff.** The stdlib classification alone does *not* catch CGNAT: CPython deliberately reports `100.64.0.0/10` as non-private (globally unreachable, but shared rather than private address space), so `addr.is_private` returns False for it. Caught by the parametrised test, which failed on that case alone. The block is now an explicit entry in `_PRIVATE_NETWORKS` — which is exactly the belt-and-braces role that list was kept for.

---

## 1. Baseline (observed, this session)

| Signal | Value | How observed |
|---|---|---|
| Test suite | **928 passed** | full `pytest` run |
| Import-linter | **1 contract kept, 0 broken** | `lint-imports` |
| Working tree | clean | `git status --short` |
| Branch | `master` (head `0ee233a`), `fix/log-redaction-and-profile-axes` pushed | `git log`, `git rev-parse` |

Every phase below is complete only when these four signals are unchanged or better. Any number in this document that is not in this table is **not** an observed result — it is a prediction, and is marked `[PENDING VERIFICATION]` where it matters.

**How the bandit signal must be scoped (correction, recorded during P3).** `pyproject.toml`'s `[tool.bandit]` excludes `tests`, `google-maps-scraper-main` and `extracted_scrapers`, but **not** `Deep-Research-With-Web-Scraping-by-LLM-And-AI-Agent-main`, and passing `-x ./tests` on the command line *replaces* that config rather than adding to it. A run invoked that way scans every vendored tree and returns 45 HIGH / 360 MEDIUM / 27090 LOW — a number about third-party code, not about this codebase. P3's merge commit says "bandit unchanged at zero medium-or-above"; that is true **of the four changed files, measured against the same files in a clean HEAD worktree**, which is the comparison that was actually run. It is not a repo-wide claim and should not be read as one. Compare per-changed-file, or fix the invocation, before quoting a bandit number again.

P3's per-file bandit delta was **+1 LOW B101** (`assert self._conn is not None` in the new `_migrate_observation_columns`), joining 17 pre-existing B101s of the identical idiom in those same files. Medium-and-above: 0 before, 0 after.

---

## 2. Architectural constraints every fix must respect

These are not style preferences; three of them are machine-enforced and will fail CI if a fix ignores them.

### 2.1 Layering (enforced: `lint-imports`)

`pyproject.toml` declares exactly one contract:

```
name = "Domain layer has zero infrastructure/application imports"
type = "forbidden"
source_modules   = ["src.domain"]
forbidden_modules = ["src.application", "src.infrastructure"]
```

Consequences for this plan:

- **`src/domain/**` fixes must stay pure** — stdlib only, no I/O, no adapters. This governs D32 (`medical_relevance.py`) and D27 (`ports.py`). Both proposed fixes are stdlib-only by construction.
- A defect whose *symptom* appears in the domain but whose *cause* is an adapter gets fixed in the adapter. No exceptions — "just import it for now" breaks the only contract the repo has.
- Ports (`src/domain/ports.py`) are the contract surface. Changing a port docstring changes a promise; changing a port signature is a breaking change for every adapter. D27 is the one item here that touches a port, and it is sequenced accordingly.

### 2.2 Ownership rule (the reason most of these are one-line fixes)

Each item below names an **owner**: the single place where the violated property is that code's responsibility. A guard added at a call site instead of at the owner leaves every sibling caller broken. Two items are explicitly *not* owner-local and say so:

- **D1** (SSRF) — one owner function, two independent transports inherit the fix for free. This is why D1 is a four-line diff and not a two-file one.
- **D2** (pagination) — owner is the API boundary; the repository floor is declared **defense-in-depth**, shipped in the same commit, not instead of the owner fix.

### 2.3 Type checking (enforced: `mypy --strict`)

`[tool.mypy] files = ["src/domain", "src/infrastructure/repositories"]`, `strict = true`.

Any change in those two trees must be fully annotated. That covers D15, D16, D27, D32 and R1/R2. Changes elsewhere (workers, `main.py`, migration script) are outside mypy's configured scope — do not add partial annotations there just because a fix touches the line; match the file's existing style.

### 2.4 Lint and security scan

- `ruff` selects `E, F, I, UP, B`, line length 120, `E501` ignored. The `ignore` list is explicitly documented as *pre-existing debt, new code should not add more*. No fix in this plan may introduce a new `E402`/`B904`/`B008` occurrence.
- `bandit` excludes `tests/`. Security-adjacent fixes in `src/` must stay bandit-clean. The established convention for a justified suppression is an inline `# nosec B608` **with a comment naming why the input is trusted** — see `observation_repository.py:141`. Follow it; do not add a bare `# nosec`.

### 2.5 Single source of truth for configuration

Config lives in `src/config_settings.py` and `tests/test_ai_ssot.py` scans source for hardcoded AI model/endpoint values. Any new tunable introduced by a fix (retryable status codes, interception caps, pool bounds) goes to the config module if it is deployment-varying, and stays a module-level constant if it is a protocol fact. Protocol facts in this plan: XSSI prefixes (D5), reserved CIDR blocks (D1), HTTP status classes (D7). None of them belong in config.

### 2.6 Dual-backend parity

Two repository implementations exist per port (SQLite/`aiosqlite` and Postgres/`asyncpg`). **A fix to one is incomplete until the sibling is checked.** D15 and D16 are entirely this class of defect. For every repository fix below, the checklist step "sibling backend audited" is mandatory even when the answer is "no change needed".

---

## 3. Phase overview

Phases are ordered by *blast radius of the defect*, not by ease. Each phase is one branch, one review, one merge.

| Phase | Theme | Items | Why here |
|---|---|---|---|
| **P0** | Trust boundaries | D1, D14, D31, D2, D22, D30 | Security boundary + one unauthenticated 500. Nothing else ships first. |
| **P1** | Corrupted deliverables | D32, D8, D35, D36, D40, D39 | Silent wrong data in output a human acts on. Highest "damage already done" risk. |
| **P2** | Dead extraction path | D5, D6, D12 | The Maps path is verified non-functional; the product's headline capability. |
| **P3** | Migration & schema | R1, R2, D3, D15, D16, D25, D26 | Data-loss class. R1/R2 first — they can break a routine upgrade today. |
| **P4** | Queue & worker reliability | D4, D13, D18, D19, D20, D21, D9 | Silent job loss and unobservable workers. |
| **P5** | Browser pool resources | D10, D11, D28 | Leaks under failure; degrade over uptime, not instantly. |
| **P6** | Adapters & residue | D7, D17, D23, D24, D27, D29, D33, D34, D37, D38 | Correctness cleanups, several with ready diffs. |

**Sequencing constraints (hard):**

- D1 before D14. Consolidating two transports onto a broken classifier just centralises the bug.
- R1/R2 before any other schema work. They fail *during boot*, which would mask later migration results.
- D5 before D6. D6's fallback only matters if the primary capture path is working; fixing the fallback first hides whether D5's fix actually landed.
- D2's repository floor lands with D2's boundary fix, never separately.

---

## 4. P0 — Trust boundaries

### D1 · SSRF guard misses whole address families — HIGH, VERIFIED-STATIC

**Property violated:** every private or reserved address is rejected at egress.
**Owner:** `src/security/ssrf_guard.is_private_ip`.
**Mechanism:** `_PRIVATE_NETWORKS` (`ssrf_guard.py:11-38`) omits `0.0.0.0/8`, `100.64.0.0/10`, `192.0.0.0/24`, `240.0.0.0/4`, `::/128`; and `addr in net` is version-strict, so `::ffff:127.0.0.1` tests False against every IPv4 entry. Both `validating_transport.py:27,81,90` and `http_client.py:129-146` import this same function and then **pin the socket** to the address it approves (`url.copy_with(host=resolved_ip)`) — so this is a live boundary bypass, not a pre-flight cosmetic.

```diff
--- a/src/security/ssrf_guard.py
+++ b/src/security/ssrf_guard.py
@@
 def is_private_ip(ip_str: str) -> bool:
     try:
         addr = ipaddress.ip_address(ip_str)
-        return any(addr in net for net in _PRIVATE_NETWORKS)
     except ValueError:
         return True  # fail closed on unparseable IPs
+
+    # An IPv4-mapped IPv6 address (::ffff:127.0.0.1) denotes the IPv4 address
+    # it wraps, but ipaddress containment is version-strict, so the CIDR list
+    # below silently answers False for it. Unwrap first.
+    mapped = getattr(addr, "ipv4_mapped", None)
+    if mapped is not None:
+        addr = mapped
+
+    # The stdlib registry covers the blocks the explicit list omits:
+    # 0.0.0.0/8 (reaches loopback on Linux), 100.64.0.0/10, 192.0.0.0/24,
+    # 240.0.0.0/4 and ::/128. The list is kept as a belt-and-braces check.
+    if (
+        addr.is_private
+        or addr.is_loopback
+        or addr.is_link_local
+        or addr.is_reserved
+        or addr.is_unspecified
+        or addr.is_multicast
+    ):
+        return True
+    return any(addr in net for net in _PRIVATE_NETWORKS)
```

**Test:** parametrised over `0.0.0.0`, `::ffff:127.0.0.1`, `100.64.0.1`, `240.0.0.1`, `::` → all True; `93.184.216.34` → False.
**Regression risk:** strictly widens denial. Existing tests assert denial, not permission. **Operational note:** this now blocks CGNAT (`100.64.0.0/10`) — correct for an SSRF guard, but confirm no legitimate scrape target is CGNAT-hosted before merge.

### D14 · Two independent SSRF transports — MED, architecture-conformance

**Property violated:** one egress boundary, one implementation.
**Files:** `src/infrastructure/http_client.py:129-146` (`GuardedTransport`) vs `src/security/validating_transport.py:64-95`.
**Mechanism:** two hand-written guarded transports exist. They disagree on async-safety — `http_client` uses blocking `socket.getaddrinfo` inside an async transport, stalling the event loop. Both inherit D1.
**Fix shape:** after D1 lands, delete `GuardedTransport` and route `http_client.get_client()` through `src/security/validating_transport`. If a behavioural difference blocks the deletion, document it in the module docstring and make the resolver non-blocking (`asyncio.get_running_loop().getaddrinfo`) — do not leave two copies silently diverging.
**First step:** enumerate every construction site of both transports before deleting either.
**Test:** one test asserting both entry points reject `http://0.0.0.0:8000/`, proving a single implementation backs both.

### D31 · `get_client(allow_private=…)` latches the first caller's value — LOW, HYPOTHESIS

**Property violated:** `allow_private` is per-caller.
**File:** `src/infrastructure/http_client.py:160-179`.
**Mechanism:** module singleton is built on first call and reused; a later caller's flag is ignored — in both directions, so a permissive first call also weakens every later strict one.
**Fix shape:** key the client cache on the flag, or make the parameter required at construction and forbid the singleton path for non-default values. Lands with D14 (same file, same review).
**Marked HYPOTHESIS:** needs a call-order reproduction to confirm a real deployment hits it.

### D2 · Negative `limit` turns a paginated read unbounded — HIGH, VERIFIED-STATIC

**Property violated:** response size is bounded by the pagination limit.
**Owner:** API boundary, `main.py:get_job_records` (`main.py:463,475`).
**Mechanism:** `limit: int = 50` has no lower bound; `min(limit, 200)` caps only the top. `record_repository.list_records` binds `limit + 1`, so `?limit=-2` binds `LIMIT -1` — **SQLite reads that as unlimited**. `has_more = len(rows) > -2` is then always True and `rows[:-2]` truncates from the wrong end. Postgres instead raises → unauthenticated 500.

```diff
--- a/main.py
+++ b/main.py
@@
-from fastapi import Body, Depends, FastAPI, HTTPException, Request
+from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
@@ async def get_job_records(
     job_id: str,
     cursor: str | None = None,
-    limit: int = 50,
+    limit: int = Query(50, ge=1, le=200),
     auth: tuple = Depends(verify_api_key),
@@
     records, next_cursor = await record_repo.list_records(
-        job_id, cursor=cursor, limit=min(limit, 200),
+        job_id, cursor=cursor, limit=limit,
     )
--- a/src/infrastructure/repositories/record_repository.py
+++ b/src/infrastructure/repositories/record_repository.py
@@ async def list_records(
+        # A non-positive limit binds LIMIT -1, which SQLite reads as
+        # "unlimited" — the opposite of a page size. Floor it at the port.
+        limit = max(1, limit)
         if cursor:
```

**Interface change (accept deliberately):** `?limit=0` or negative now returns **422 instead of 200**. No in-repo caller sends one.
**Sweep required:** grep every other `limit:`/`offset:` query parameter in `main.py` for the same missing `ge=`. Fix them in the same commit — the defect class, not the one instance.

### D22 · Non-ASCII Authorization header returns 500, not 401 — MED, VERIFIED-STATIC

**Property violated:** `verify_admin_key` terminates in either 401 or success. Confirmed at `src/auth_middleware.py:410`.
**Mechanism:** ASGI decodes headers as latin-1, so a header byte above 0x7F yields a non-ASCII `str`. `hmac.compare_digest(str, str)` raises `TypeError: comparing strings with non-ASCII characters is not supported` — unhandled, so an *unauthenticated* request produces a 500 and a stack trace instead of a clean 401.
**Fix shape:** compare bytes, not str — encode both sides once (`.encode("utf-8", "surrogateescape")`) before `compare_digest`. Keeps constant-time comparison, which the docstring at `auth_middleware.py:400` explicitly promises.
**Sweep required:** every other `compare_digest` call on header-derived input in this module gets the same treatment.
**Test:** `Authorization: Bearerké` → 401, not 500.

### D30 · Denylist wildcard fails open — LOW, HYPOTHESIS

**Property violated:** denylist entries are enforced.
**File:** `src/security/url_policy.py:62-67`.
**Mechanism:** only `*.suffix` form is handled; an entry like `evil*.com` matches nothing and silently permits.
**Sequencing:** **reproduce first.** Confirm whether any shipped config or documented example uses a non-`*.suffix` pattern. If none does, the fix is still worth it (a future operator will write one), but it drops to the bottom of P0 and ships as: reject unsupported patterns loudly at config-load time rather than silently matching nothing. Fail-closed beats fail-silent.

---

## 5. P1 — Corrupted deliverables

### D32 · Veterinary practices graded as confirmed medical, then listed as doctors — HIGH, **VERIFIED-EXEC**

**Property violated:** with `include_veterinary=False`, no veterinary practice is graded medical — the explicit intent of commit `aed1e70` ("fix: keep vets out of doctor results").
**Owner:** `src/domain/medical_relevance.medical_signal`, `medical_relevance.py:175-184`. Pure function, domain layer, no imports change.

**Executed against the current tree (pre-fix):**

```
'Κτηνιατρική Κλινική Περαίας'   → ('confirmed', 'name:κλινικ')   → specialty Κτηνίατρος
'Κτηνιατρικό Διαγνωστικό Κέντρο' → ('confirmed', 'name:διαγνω')
'Κτηνιατρείο Περαίας'           → ('excluded', None)             ← control, already handled
'Ιατρείο Περαίας'               → ('confirmed', 'name:ιατρ')     ← human practice, must stay confirmed
```

**Mechanism:** `_VET_RE` (pattern `κτηνιατρ`) is consulted **only** inside `if include_veterinary:`. With the flag False nothing subtracts, so `_NAME_RE` reaches the name first and confirms on a *different* alternative — `κλινικ`, `διαγνω`, `χειρουργ`, `clinic`. The `(?<!κτην)ιατρ` lookbehind defends exactly one spelling (`Κτηνιατρείο`) and nothing else. `veterinary_care` is also absent from `NON_MEDICAL_PLACE_TYPES`, so no later tier recovers it.

```diff
--- a/src/domain/medical_relevance.py
+++ b/src/domain/medical_relevance.py
@@
     # A name that says "ktiniatreio" is definitive, so it outranks the type
     # list the way the human-practice vocabulary does.
-    if include_veterinary:
-        vet = _VET_RE.search(folded)
-        if vet:
-            return "confirmed", f"name:{vet.group(0)}"
+    # Definitive in both directions: when vets are excluded this must still
+    # run, because the practice vocabulary below matches a *different*
+    # alternative ("kliniki", "diagnostiko") that the (?<!ktin) lookbehind in
+    # _NAME_RE never sees — so a vet clinic was graded confirmed and then
+    # labelled Ktiniatros inside the doctors list.
+    vet = _VET_RE.search(folded)
+    if vet:
+        if include_veterinary:
+            return "confirmed", f"name:{vet.group(0)}"
+        return "excluded", f"name:{vet.group(0)}"
```

**Safety proof (executed):** `_VET_RE` matches all three vet names and **neither** `Ιατρείο Περαίας` nor `Οδοντιατρείο Καρδίας` — so excluding on it cannot catch a human practice.
**Test:** parametrised exclusion test over the three vet spellings + a positive control for `Ιατρείο`/`Οδοντιατρείο`.

**Dropped during implementation — adding `veterinary_care` to `NON_MEDICAL_PLACE_TYPES`.** The plan originally called for this as defense-in-depth. Executed against the tree: `medical_signal("Alfa", ["veterinary_care"])` already returns `('excluded', None)` with the flag off, because `veterinary_care` is in neither `MEDICAL_PLACE_TYPES` nor `AMBIGUOUS_PLACE_TYPES` and falls through to the default. Adding it to `NON_MEDICAL_PLACE_TYPES` would have *broken* `test_a_bare_veterinary_type_is_only_worth_a_review`, since the non-medical loop runs before the veterinary one and would return `excluded` even with `include_veterinary=True`. The name check is the entire defect.

### D8 · Rating clamp fabricates a 1-star rating — MED

**Property violated:** a clamp must not fabricate values.
**Owner:** `src/extractors/strategies/google_maps.py:245` (`_entry_to_data`).
**Mechanism:** `float(max(1.0, min(5.0, rating)))`. Google emits `0` for an unrated place → `1.0`. `isinstance(False, int)` is True, so a `False` in that slot also becomes 1.0. Output is indistinguishable from a genuine one-star business.

```diff
--- a/src/extractors/strategies/google_maps.py
+++ b/src/extractors/strategies/google_maps.py
@@
                 rating = sub4[7]
-                if isinstance(rating, (int, float)):
-                    data["rating"] = float(max(1.0, min(5.0, rating)))
+                # Google sends 0 for an unrated place. Clamping it to 1.0 would
+                # publish a fabricated one-star rating, so drop the field
+                # instead — bool is excluded because isinstance(False, int).
+                if isinstance(rating, (int, float)) and not isinstance(rating, bool):
+                    if 1.0 <= rating <= 5.0:
+                        data["rating"] = float(rating)
```

**Consistent with the file's own convention:** omit rather than fabricate (`if not data.get("name"): continue`).
**Sweep required:** every other `max(…, min(…))` clamp in the extractors tree — same defect class, likely more instances (`review_count`, price level).

### D35 · CSV formula injection in exported leads — MED

**Property violated:** a CSV cell renders as its value.
**Owner:** `lead_export.py:264` (writer), not the caller.
**Mechanism:** the file is BOM'd for Excel (`lead_export.py:243`), and `internationalPhoneNumber` values start with `+` (`+30 2392 021745`), which Excel parses as a formula. `name` and `address` are equally unguarded against `=`, `+`, `-`, `@` — and a business controls its own name.
**Fix shape:** one escaping helper applied at the single write point: prefix a cell with `'` when it starts with `= + - @ \t \r`. Owner-local, one function, every column routed through it.
**Test:** a row whose name is `=cmd|' /c calc'!A0` round-trips through the exporter with a leading apostrophe.
**Evidence note:** the *missing guard* is VERIFIED-STATIC; how Excel renders it is HYPOTHESIS — `exports/` is empty, so no real file was inspected.

### D36 · Merge note names the wrong absorbed row — MED

**Property violated:** the merge note names what the row actually absorbed.
**File:** `lead_export.py:250`.
**Mechanism:** `dict(result.merged)` is keyed by **name** and keeps only the last dropped row per winner. A kept row sharing a name with an unrelated dropped row inherits a false "Συγχωνεύτηκε με" annotation.
**Fix shape:** key the merge record by the same identity the deduper uses (the stable record id), and accumulate a list rather than overwriting.

### D40 · `#` in an exclusion entry truncates the rule — LOW

**Property violated:** an exclusion entry matches what the operator typed.
**File:** `lead_export.py:102`.
**Mechanism:** `split("#", 1)[0]` is comment-stripping applied to data. `Ιατρείο #1` becomes the prefix `Ιατρείο`, excluding **every** practice so named.
**Fix shape:** strip comments only for whole-line comments (line starts with `#`) or require whitespace before the `#`. Trailing-whitespace strip must run after.

### D39 · Reported locality may not be the row's locality — LOW, HYPOTHESIS

**File:** `place_sweep.py:515-523`. `named` is ordered by `config.areas`, not by the row's locality field, so an area label can be attached to a row from a neighbouring area — reported with full confidence.
**Sequencing:** reproduce against a stored sweep result before changing anything. **This is the most likely false positive in the whole audit** — it assumes `named` ordering is the only ordering applied. Confirm, then fix or close it explicitly in this document.

---

## 6. P2 — Dead extraction path

### D5 · Engine discards the payloads the Maps strategy exists to parse — HIGH, **VERIFIED-EXEC**

**Property violated:** interception captures the payloads strategies consume.
**Owner:** `src/infrastructure/browser/engine.py:32-36,125` — interception is infrastructure's contract; the strategy is correct and stays untouched.
**Mechanism:** `_is_json_content_type` accepts only media types ending in `json`. Google serves Maps search results as `text/javascript` behind an XSSI `)]}'` prefix — which `google_maps.py:170` explicitly strips, proving the strategy expects exactly that body. The two halves of one feature disagree.
**Observed live this session:** `json_payloads=0` pre-consent, `1–2` post-consent, and the only captured payload was `…/maps/vt/pb=…` vector-tile data. A widened listener collected 2–6 additional `)]}'`-prefixed bodies the engine had dropped. Strategy output: **0 records on all three runs.**

Two variants. **Ship the architectural one.**

<details>
<summary>MINIMAL (rejected — buffers every JS bundle on every site)</summary>

```diff
 def _is_json_content_type(content_type: str) -> bool:
     media_type = content_type.split(";", 1)[0].strip()
-    return media_type.endswith("json")
+    return media_type.endswith("json") or media_type in (
+        "text/javascript", "application/javascript",
+    )
```
</details>

```diff
--- a/src/infrastructure/browser/engine.py
+++ b/src/infrastructure/browser/engine.py
@@
+# Responses that are JSON in everything but their Content-Type: an XSSI guard
+# prefix marks a body that is a JSON array, not executable script.
+_XSSI_PREFIXES = (b")]}'", b")]}\n", b"while(1);")
+_JS_CONTENT_TYPES = ("text/javascript", "application/javascript")
+
+
 def _is_json_content_type(content_type: str) -> bool:
     media_type = content_type.split(";", 1)[0].strip()
     return media_type.endswith("json")
@@
             content_type = response.headers.get("content-type", "").lower()
-            if not _is_json_content_type(content_type) or not response.ok:
+            media_type = content_type.split(";", 1)[0].strip()
+            is_json = _is_json_content_type(content_type)
+            maybe_xssi = media_type in _JS_CONTENT_TYPES
+            if not (is_json or maybe_xssi) or not response.ok:
                 return
@@
             body = await response.body()
+            if maybe_xssi and not is_json:
+                if not body.lstrip()[:16].startswith(_XSSI_PREFIXES):
+                    return  # ordinary script, not a disguised JSON payload
```

**Why architectural:** the minimal variant changes memory behaviour on *every* site, not just Maps. The XSSI sniff keeps the existing caps (2 MB/response, 200/page, 20 MB total) meaningful.
**Test:** an XSSI-prefixed `text/javascript` body is captured; a plain `(function(){…})()` body is not.
**Acceptance (must be executed, not assumed):** re-run the Perea monastery query end-to-end through `cli.py scrape --browser` and confirm `record_count > 0` from the JSON path — not from the DOM workaround.

### D6 · HTML fallback returns the bootstrap blob, not the results array — MED, **VERIFIED-EXEC**

**Property violated:** the HTML fallback yields the results array.
**File:** `src/extractors/strategies/google_maps.py:131-152`.
**Mechanism:** `_extract_embedded_json`'s regex returns `window.APP_INITIALIZATION_STATE` (35,267 bytes observed) whose shape is not `data[0][1][i][14]` → 0 records.
**Fix shape:** after D5 lands, decide deliberately: either teach the fallback to walk into the initialisation state's nested search payload, or **delete the fallback** and let the strategy report "no payload" honestly. A fallback that always returns zero is worse than none — it converts a diagnosable failure into a silent empty result.
**Recommendation:** delete unless a concrete shape can be verified live. Sequenced after D5 so the primary path proves itself first.

### D12 · Malformed payload shape crashes the whole job — MED

**Property violated:** parsing untrusted shapes must not raise.
**File:** `src/extractors/strategies/google_maps.py:113`.
**Mechanism:** `payload[0] == [None]` → `container=None` → `len(None)` TypeError → propagates out of extraction → whole job fails, downstream stages skipped.
**Fix shape:** the shape-walk in `_find_business_arrays` gets one guard clause per index assumption, returning "no businesses" instead of raising. Owner-local; the strategy is the only thing that knows this shape.
**Test:** feed `[[None]]`, `[]`, `[{}]` and assert an empty result, not an exception.

---

## 7. P3 — Migration & schema

### R1 · Concurrent boot fails the SQLite profile migration — **NEW** (external review, verified)

**Source:** Codex review on commit `0ee233ac7b`. **Verified against source before acceptance** — the finding is correct.
**Property violated:** a concurrent upgrade serialises; `initialize()` either migrates or no-ops, never crashes.
**File:** `src/infrastructure/repositories/observation_repository.py:163-176`.

**Mechanism (confirmed):**

1. `_migrate_profile_columns` probes `PRAGMA table_info(domain_profiles)` at line 164 — a **read**, taking no write lock.
2. `await self._conn.execute("BEGIN")` at line 169 opens a **DEFERRED** transaction; SQLite acquires the write lock only at the first write, i.e. the `ALTER` at line 172.
3. `boot.py` starts multiple processes with `asyncio.create_subprocess_exec` (API at minimum, plus `worker_scraper.py` at `boot.py:51`), each opening its own connection to the same file on the default SQLite backend.
4. Both can complete step 1 against the legacy schema. The loser reaches its `ALTER` after the winner committed, and raises `duplicate column name: preferred_fetch_tier` — propagating out of `initialize()` and killing that process during a routine upgrade.

The existing `throttle_delay_ms` migration at lines 117-122 is *not* vulnerable — it is wrapped in `try/except Exception: pass`. The newer, more careful migration is the one that regressed.

```diff
--- a/src/infrastructure/repositories/observation_repository.py
+++ b/src/infrastructure/repositories/observation_repository.py
@@ async def _migrate_profile_columns(self) -> None:
         assert self._conn is not None
-        async with self._conn.execute("PRAGMA table_info(domain_profiles)") as cursor:
-            existing = {row["name"] for row in await cursor.fetchall()}
-        if "preferred_fetch_tier" in existing:
-            return
-
-        await self._conn.execute("BEGIN")
+        # boot.py starts several processes against the same SQLite file, so the
+        # probe and the ALTER must happen under the same write lock. BEGIN is
+        # DEFERRED -- it takes no lock until the first write -- so a probe
+        # outside it can observe the legacy schema, lose the race, and then
+        # raise "duplicate column name" out of initialize().
+        await self._conn.execute("BEGIN IMMEDIATE")
         try:
+            async with self._conn.execute("PRAGMA table_info(domain_profiles)") as cursor:
+                existing = {row["name"] for row in await cursor.fetchall()}
+            if "preferred_fetch_tier" in existing:
+                await self._conn.execute("ROLLBACK")
+                return
             await self._conn.execute(
                 "ALTER TABLE domain_profiles ADD COLUMN preferred_fetch_tier TEXT NOT NULL DEFAULT 'http'"
             )
```

**Also required:** confirm the connection's busy timeout is non-zero, or `BEGIN IMMEDIATE` raises `database is locked` instead of waiting. `aiosqlite.connect` inherits `sqlite3`'s 5 s default; if `initialize()` is later changed to pass `timeout=0`, this fix silently reverts to failing. Add a `PRAGMA busy_timeout` next to the existing `journal_mode`/`synchronous` pragmas at lines 111-112 so the dependency is explicit rather than inherited.
**Test:** two `ObservationRepository` instances over one temp DB file, both `initialize()`d concurrently via `asyncio.gather`, against a legacy-schema fixture — both must succeed and the column must exist exactly once.
**Note on the atomicity comment at lines 154-161:** it stays true and stays in place. This fix does not weaken it — it extends the same transaction to cover the probe.

### R2 · Same race in `_migrate_observation_columns` — **NEW** (sibling, found while verifying R1)

**File:** `src/infrastructure/repositories/observation_repository.py:131-143`.
**Mechanism:** identical shape — `PRAGMA table_info(strategy_observations)` outside any transaction, then unguarded `ALTER TABLE … ADD COLUMN` per missing column. No `try/except`, no lock. A concurrent boot raises `duplicate column name` here too.
**Evidence: VERIFIED-EXEC.** Four concurrent `initialize()` calls over one legacy database, against the unfixed tree:

```
AssertionError: [OperationalError('duplicate column name: groundedness'),
                 OperationalError('duplicate column name: preferred_fetch_tier')]
```

Both migrations fail, not just the one the review named — R2 was upgraded from "sibling by inspection" to executed evidence by the same test.
**Fix shape:** same `BEGIN IMMEDIATE` + re-probe pattern. Since both migrations run from the same `initialize()` (lines 125-126), the cleanest form is one helper both call, rather than two copies of the pattern.
**Why this belongs in the plan:** the external review named one instance; the defect is the *pattern*. Fixing only the reported line leaves the identical failure one function above it.

### R3 · Concurrent first boot crashes at the WAL switch — **NEW**, VERIFIED-EXEC

**Found by:** the R1/R2 regression test failing under full-suite load, after R1/R2 had already been merged. Not part of P2; recorded here because it is the same boot path.

**Property violated:** `initialize()` either prepares the connection or fails for a reason the operator can act on — a concurrent boot is not one.
**File:** `src/infrastructure/repositories/observation_repository.py:111` (as merged in `d79de08`).

**Mechanism:** switching journal mode requires a lock no other connection holds, and SQLite answers `SQLITE_BUSY` for it **immediately** rather than honouring `busy_timeout`. `boot.py` starts the API and the scraper together, so on the first boot against a non-WAL file they race at `PRAGMA journal_mode=WAL` — one statement *before* the migrations the external review flagged. The `busy_timeout` added with R1/R2 was also set *after* this line, so it protected nothing here.

**Evidence, executed:** a standalone reproducer running five rounds of four concurrent `initialize()` calls against a legacy file, with the traceback filtered to the failing statement:

```
attempt 0: OperationalError: database is locked
   failing statement lines: ['await self._conn.execute("PRAGMA journal_mode=WAL")']
   ... 7 occurrences ...
TOTAL FAILURES across 5 attempts x 4 conns: 7
```

After the fix, the same reproducer: `TOTAL FAILURES ... : 0`.

**Fix:** set `busy_timeout` first, before anything that can contend, and raise it to 30 s — a boot-time migration that waits beats one that crashes. Tolerate `SQLITE_BUSY` on the journal-mode switch itself, since WAL is an optimisation rather than a correctness requirement and a concurrent converter will finish the job.

**Test:** the R1/R2 concurrency test now runs three rounds. One round of four caught this only about 60% of the time — which is exactly why it passed in isolation and failed under load. A probabilistic race needs a repeated guard, or the guard is theatre.

**Lesson recorded:** R1/R2 were reported as verified on the strength of an isolated run. That claim was true and insufficient — the isolated run could not see this. Concurrency fixes need the loaded suite before they are called done.

### D3 · Migration reports rows it never wrote — HIGH

**Property violated:** reported counts reflect rows actually written.
**File:** `migrate_sqlite_to_postgres.py:282-318`.
**Mechanism:** `inserted`/`updated` increment at lines 291-294, **before** `session.execute(stmt)` at 312. The `except` at 314-315 logs and continues without touching `stats.errors`; line 317 commits and 318 returns inflated counts. Worse than one row: once a statement errors, the session's transaction is aborted, so **every subsequent row in the batch also fails** while the summary still claims a full copy.

```diff
--- a/migrate_sqlite_to_postgres.py
+++ b/migrate_sqlite_to_postgres.py
@@
                 exists = result.scalar_one_or_none() is not None
-
-                if exists:
-                    updated += 1
-                else:
-                    inserted += 1
-
                 # Upsert
                 stmt = pg_insert(OpportunityModel).values(data)
@@
                 await session.execute(stmt)
+                # Count only what actually landed: an aborted statement below
+                # used to be reported as a migrated row.
+                if exists:
+                    updated += 1
+                else:
+                    inserted += 1
 
             except Exception as e:
                 logger.error(f"Error upserting opportunity {data.get('id')}: {e}")
+                self.stats.errors += 1
+                # The session's transaction is aborted after a failed
+                # statement; continuing would silently lose the rest of the
+                # batch too.
+                raise
```

**BREAKING, declared:** the `raise` is a deliberate behaviour change — a migration that previously "succeeded" with partial data now fails loudly. This is the point. Document it in the migration runbook and verify the dry-run path still exits cleanly.
**Sweep required:** the same count-before-write shape in every other `_migrate_*` method in this file.

### D15 · `last_seen` assigned twice in one UPDATE — MED

**File:** `src/infrastructure/repositories/postgres_record_repository.py:132-152`.
**Property violated:** the same port behaves alike on both backends. Postgres raises `42601`; SQLite accepts the statement.
**Fix shape:** build the SET clause from a dict so a column cannot be emitted twice. Under `mypy --strict` (this tree is in scope) — annotate fully.
**Parity check:** verify the SQLite sibling for the same double-assignment before closing.

### D16 · Postgres observation table silently drops two fields — MED

**File:** `src/infrastructure/repositories/postgres_observation_repository.py:19-35`.
**Property violated:** `StrategyObservation` round-trips through its port.
**Mechanism:** the PG table lacks `groundedness` and `citation_coverage`; `synthesis_service.py:176` writes them → dropped on PG only. The SQLite side already migrated these (`_MIGRATION_COLUMNS`, `observation_repository.py:36-40`).
**Fix shape:** add the columns to the PG DDL **plus** an idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS` migration for existing deployments. Postgres supports `IF NOT EXISTS` natively, so R1's race does not apply here.
**Test:** a round-trip test that asserts every `StrategyObservation` field survives, run against both backends — this test is the real fix; the columns are just its consequence.

### D25 · Dead-letter migration is not idempotent — LOW

**File:** `migrate_sqlite_to_postgres.py:417`. `uuid4()` per row, no `on_conflict_do_nothing` → every re-run duplicates the entire dead-letter table.
**Fix shape:** derive a deterministic id from the source row's natural key, or add `on_conflict_do_nothing` on that key. Re-running a migration after a failure is normal operator behaviour — it must be safe.

### D26 · CLI arguments silently do nothing — LOW

**File:** `migrate_sqlite_to_postgres.py:128,689`. An unknown `--tables` name is dropped silently (migrates nothing, reports success); `--verify` is parsed and never read.
**Fix shape:** validate `--tables` against the known set and exit non-zero on an unknown name; either implement `--verify` or remove the flag. A flag that parses and does nothing is worse than an absent one.

---

## 8. P4 — Queue & worker reliability

### D4 · Discovery enqueues to a queue nobody reads — HIGH (guarded by `features.discovery=False`)

**File:** `worker_discovery.py:150`. RPUSHes to a **list** `jobs_queue`; the scraper `XREADGROUP`s a **stream** `jobs_stream`. `poll_jobs` has zero call sites.
**Property violated:** an enqueued job is eventually consumed.
**Fix shape:** publish through the same `stream_queue` port the scraper consumes. The feature flag is what makes this MED-in-practice — but it means the feature is *dead*, not merely buggy, so enabling the flag would ship a silent black hole.
**Decide explicitly:** fix it, or delete `poll_jobs` and the RPUSH path. Do not leave a third queue implementation in the tree.

### D13 · A claimed pending message can never make progress — MED

**File:** `src/infrastructure/queues/stream_queue.py:326`. The XCLAIM result is re-wrapped into `QueueMessage(**{})` → `ValidationError`, which is outside the caught tuple → the orphan is re-claimed every 60 s, forever.
**Fix shape:** map the XCLAIM reply shape correctly (it differs from XREADGROUP's), and widen the caught exception to include `ValidationError` with a dead-letter path — an un-parseable message must leave the pending set, not spin.
**Test:** a malformed pending entry is dead-lettered after N claims rather than re-claimed indefinitely.

### D18 · Reporter acks on total delivery failure — MED

**File:** `worker_reporter.py:63-80`. `asyncio.gather` results are discarded and the generator swallows exceptions → a run where every delivery failed acks as success. Separately, synchronous pandas I/O runs on the event loop.
**Fix shape:** `gather(..., return_exceptions=True)`, inspect results, ack only on success, nack/dead-letter otherwise. Move the pandas call to `asyncio.to_thread`.
**This is two defects in one function**; split into two commits if the diff gets wide.

### D19 · URL marked seen before its children are enqueued — MED

**File:** `worker_processor.py:133`. `seen.update` precedes fan-out; a raise after it means redelivery computes an empty `fresh_follows` → those children are never enqueued by anyone.
**Fix shape:** mark seen after successful enqueue, or make the pair atomic. Ordering-only change; no new abstraction.

### D20 · `transaction()` is not atomic on SQLite — MED

**File:** `src/infrastructure/repositories/job_repository.py:119-133`. Yields a process-wide shared connection, so a concurrent `heartbeat` write commits a half-written unit of work.
**Fix shape:** a per-transaction connection, or an `asyncio.Lock` around the shared one. In scope for `mypy --strict`.
**Risk:** this is the highest-regression-risk item in P4 — it touches the connection lifecycle every repository method depends on. Land it alone, not batched.

### D21 · Postgres purge is not atomic — MED

**File:** `src/infrastructure/repositories/postgres_job_repository.py:291-296`. A fresh pooled connection per statement → each DELETE autocommits alone, so a crash between them leaves orphaned attempts.
**Fix shape:** acquire one connection and run both DELETEs in one transaction. Parity item with D20 — same property, opposite backend.

### D9 · Three workers discard every metric they record — MED

**Files:** `worker_processor.py`, `worker_reporter.py`, `worker_discovery.py`. `metrics_tracker.initialize()` is called only in `main.py:85` and `worker_scraper.py:606`, so the other three record into an uninitialised tracker — SLA alerts cannot fire for them.
**Fix shape:** initialise in the shared worker entry path rather than per worker, so a fourth worker added later inherits it. If no shared entry path exists, that absence is the actual finding — note it and add one.

---

## 9. P5 — Browser pool resources

### D10 · Context leaked when `add_init_script` raises — MED

**File:** `src/infrastructure/browser/pool.py:240`. The context is created, then `add_init_script` raises before it is assigned → referenced by nobody, and `engine.close()` sees `self.context` unset, so it is never closed.
**Fix shape:** `try/except` around post-creation setup that closes the orphan before re-raising.

### D11 · Failed `initialize()` launches a second Chromium — MED

**File:** `src/infrastructure/browser/pool.py:170-198`. A raise before `_is_initialized = True` leaves the first browser running and unreferenced; the next `acquire()` launches another.
**Fix shape:** set the flag in a `finally`, or tear down what was built on the failure path. **Both D10 and D11 are the same class** — partially-constructed resource with no owner — so fix them in one commit with one test each.

### D28 · Pool queue path is dead — LOW, reachability UNKNOWN

**File:** `src/infrastructure/browser/pool.py:280-327`. The only caller always passes a fingerprint, so the queue path never executes: warm contexts are never leased, there is no backpressure, and storage is never cleared.
**Fix shape:** **investigate before touching.** Either the fingerprint path should also use the queue (then `pool_size` currently bounds nothing — a real capacity defect), or the queue is genuinely obsolete (then delete it). Deleting dead code is the preferred outcome; a pool whose size parameter does nothing is a trap for the next operator.

---

## 10. P6 — Adapters & residue

### D7 · OpenRouter retries permanent errors and destroys the provider's message — MED

**File:** `src/infrastructure/ai/openrouter.py:153-198`.
**Mechanism:** `_call` posts and calls `response.json()` with **no status check**; `http_client.post` (lines 62-64) does not `raise_for_status`, and httpx does not raise on 4xx. A 401 body has no `choices` → `_extract_text` returns None → `ValueError("unparseable response…")` → the broad `except` retries. With `max_retries=3, base_delay_s=1.0` that is **3 billed POSTs and ~3 s of sleeps per call**, and the breaker records `"unparseable response"` instead of `401 invalid api key`.

```diff
--- a/src/infrastructure/ai/openrouter.py
+++ b/src/infrastructure/ai/openrouter.py
@@
                     )
+                    # httpx does not raise on 4xx/5xx. Without this, an auth or
+                    # bad-model error parses as "no choices" and is retried as
+                    # if transient, three times, with the real cause discarded.
+                    if response.status_code >= 400:
+                        detail = response.text[:300]
+                        if response.status_code in (408, 429) or response.status_code >= 500:
+                            raise RuntimeError(
+                                f"OpenRouter {response.status_code} (retryable): {detail}"
+                            )
+                        self._record_failure(
+                            RuntimeError(f"OpenRouter {response.status_code}: {detail}")
+                        )
+                        logger.error(
+                            f"OpenRouter job={profile.job.value} failed permanently "
+                            f"with {response.status_code}: {detail}"
+                        )
+                        return None
                     data = response.json()
```

**Test:** a 401 returns None after exactly **one** request.
**Sweep required:** `http_client.post` not calling `raise_for_status` is the shared cause — audit every other caller of it for the same assumption before deciding whether to fix the adapter or the client. If more than one caller assumes raising, fix the client and keep this adapter's classification.

### D17 · Cache hit counters are WRONGTYPE on every call, and the test mocks the broken method — MED

**File:** `src/smart_crawler.py:235,249,263-267`.
**Mechanism:** `setex(key, ttl, json.dumps(data))` writes a **string**; `_increment_cache_hit` then calls `hincrby(key, "hit_count", 1)` — a **hash** command on the same key → `WRONGTYPE` on every call, swallowed by `except Exception: pass`.
**Aggravating:** `tests/test_cache.py:42,64,97` set `crawler._increment_cache_hit = AsyncMock()`, so the suite asserts the counter is *called*, never that it *works*. The test actively conceals the defect.
**Fix shape:** store hit counts under a separate key (`{key}:hits`) with its own TTL, or store the payload as a hash field so both commands agree. Then **remove the mock** and assert the counter's real value — a test that mocks the unit under test is not covering it.
**Do both or neither:** fixing the code while leaving the mock in place means the next regression is equally invisible.

### D23 · Correlation IDs are always None — LOW

**File:** `main.py:337`. `CorrelationIDMiddleware` is defined but never added to `app`, so tracing silently always yields `None`.
**Fix shape:** `app.add_middleware(...)`. One line. Verify ordering relative to the auth middleware.
**Test:** a request with `X-Correlation-ID` reaches a job record carrying it — otherwise this regresses invisibly again.

### D24 · Optional config key raises `KeyError` — LOW

**File:** `spacescraper.py:50`. `source['target_site']` raises, while line 54 defaults the same key — the file contradicts itself four lines apart.
**Fix shape:** `source.get('target_site')` with the same default line 54 uses.

### D27 · Port docstring promises an ordering the implementation does not provide — LOW

**Property violated:** `RecordRepository.list_records` returns records "ordered by `created_at` ASC" (`src/domain/ports.py:165`).
**Confirmed:** `record_repository.py:108,116` order by `record_id ASC`. The index `idx_records_job_created` on `(job_id, created_at)` (line 36) is therefore never used by this query.
**Two honest options:**
1. **Correct the contract** — change the docstring to `record_id ASC`. One line, zero risk, but the index stays dead and API consumers lose chronological ordering they may already rely on.
2. **Honour the contract** — make the cursor composite `(created_at, record_id)` and order by both, using the existing index. The cursor is opaque, so its format may change; **must be mirrored in `postgres_record_repository.py`** (§2.6).
**Recommendation:** option 2. The index's existence is evidence of the original intent, and chronological ordering is what an API consumer paging a job's records actually wants. Sequenced last in P6 because it touches a port contract and both backends.

### D29 · One malformed JSON-LD item abandons the rest of its block — LOW

**File:** `src/application/extraction_pipeline.py:353`. An `@type` given as a list raises `AttributeError`, which is swallowed at block level → the remaining valid items in that block are never parsed.
**Fix shape:** handle `@type` as `str | list[str]` (the JSON-LD spec permits both), and narrow the `except` to per-item scope so one bad item cannot discard its siblings.

### D33 · A mid-sweep failure discards every paid result before it — MED

**File:** `place_sweep.py:437`. `resolve_area_center` is the one unguarded call in the loop; a raise in area 2 discards area 1's ~30–100 already-billed results, and reports nothing.
**Fix shape:** per-area `try/except` that records the failure and continues, with failed areas named in the final report. Billed work must never be discarded silently.

### D34 · `PlacesQuotaError` is raised and never handled — MED

**File:** `src/infrastructure/places/google_places.py:195`. Four references repo-wide, **no consumer outside tests**; a broad `except Exception` demotes it, so quota exhaustion still fires every remaining request and the report reads "few practices here" at `EXIT_OK`.
**Fix shape:** catch it at the sweep loop, abort the remaining areas, and exit non-zero with an explicit quota message. An exhausted-quota run must not be indistinguishable from a sparse area.

### D37 · Page-full detection counts parsed results, not returned ones — MED, HYPOTHESIS

**File:** `src/infrastructure/places/google_places.py:244`. The "full page → subdivide" flag counts *parsed* results; one entry dropped by `from_api` turns 20 into 19 → no subdivision → businesses silently missing.
**Sequencing:** needs a live Places response to settle. Reproduce with a recorded fixture containing one unparseable entry before changing the threshold logic.

### D38 · Cross-pass backfill drops the dedup key — MED, HYPOTHESIS

**File:** `place_sweep.py:320-323`. Only `website` is backfilled across passes; `phone` — which is both the dedup key **and** the product — is not, so one practice can split into two call rows.
**Sequencing:** reproduce against a stored two-pass result first.

---

## 11. Execution protocol

Per phase:

1. Branch from `master`: `fix/audit-p{N}-{slug}`.
2. Apply the phase's items. **One logical fix per commit** — a reviewer must be able to revert D8 without reverting D32.
3. Commit message: `fix(<area>): <property restored>` — describe the property, not the code. Existing history follows this (`fix(security): redact credentials in any URL, not just postgres DSNs`). No attribution lines.
4. Run the full gate:
   ```bash
   pytest && lint-imports && ruff check . && mypy && bandit -r src -q
   ```
5. Compare against §1 baseline. A phase that reduces the passing count is not done.
6. Merge to `master` with a merge commit; verify with `git rev-list --parents -n 1 HEAD` (two parents), not `git log` alone — the RTK proxy filters `git log` output and has already misreported a merge once in this project.

**Do not batch phases.** P0 and P3 both change behaviour that other phases' tests depend on.

---

## 12. Regression-test requirements

Every item ships a test that **fails against the current tree**. A test written after the fix that passes both before and after is not a regression test — it is decoration.

Specific obligations:

| Item | The test must fail on current `master` by |
|---|---|
| D1 | asserting `is_private_ip("::ffff:127.0.0.1") is True` |
| D2 | asserting `?limit=-2` → 422 |
| D3 | asserting `stats.errors == 1` after a failing write |
| D5 | asserting an XSSI `text/javascript` body is captured |
| D8 | asserting `"rating" not in` the result for Google's `0` |
| D17 | asserting the real counter value **with the `AsyncMock` removed** |
| D32 | asserting `medical_signal("Κτηνιατρική Κλινική Περαίας", …, include_veterinary=False)[0] == "excluded"` |
| R1/R2 | two concurrent `initialize()` calls over one legacy-schema file both succeeding |

D17's entry is the important one: **removing the mock is part of the fix.** Three existing tests currently pass *because* they mock the broken method.

---

## 13. Items requiring reproduction before any edit

These five are HYPOTHESIS. Changing code on a hypothesis is how a clean audit becomes a regression.

| Item | What must be observed first |
|---|---|
| D30 | whether any shipped or documented denylist entry uses a non-`*.suffix` wildcard |
| D31 | a call-order trace showing `get_client` reused across differing `allow_private` |
| D37 | a recorded Places response containing an entry `from_api` drops |
| D38 | a two-pass sweep result where a phone is present in one pass only |
| D39 | a stored sweep where `named` ordering and the locality field disagree |

If reproduction fails, **close the finding in this document with the negative result recorded.** A finding that cannot be reproduced and is quietly deleted teaches the next audit nothing.

---

## 14. Risk register

| Risk | Item | Mitigation |
|---|---|---|
| CGNAT-hosted legitimate target now blocked | D1 | Confirm no target in `100.64.0.0/10` before merge |
| Previously-"successful" partial migrations now fail loudly | D3 | Declared BREAKING; runbook note; dry-run path verified |
| `?limit=0` callers get 422 | D2 | No in-repo caller; note in API changelog |
| Connection-lifecycle change destabilises every repository method | D20 | Lands alone, not batched; full suite before and after |
| Interception memory growth on JS-heavy sites | D5 | Architectural variant + existing caps (2 MB / 200 / 20 MB) |
| Opaque cursor format change breaks a live paging client | D27 | Option 2 only after confirming no external consumer stores cursors |
| Fixing D6 by deletion removes a path someone relies on | D6 | Sequenced after D5 proves the primary path works |

---

## 15. Coverage boundary

**Audited:** security, API boundary, extraction strategies, browser engine and pool, queues, repositories (both backends), workers, migration script, AI adapter, Places cluster.

**Not audited — a clean result here is unscoped:** `src/application/{pipeline,evaluator,synthesis_service,adaptive_fetch,rendering_policy,strategy_selector}.py`, `src/domain/models.py` and `ports.py` beyond docstrings, `src/database_models.py`, `scripts/`, vendored trees (`google-maps-scraper-main`, `extracted_scrapers`, `Deep-Research-*`).

**Highest-value next audit target:** `src/application/evaluator.py` + `strategy_selector.py` — the remaining unaudited decision logic, and the code that writes back into `domain_profiles` (the same table R1 migrates).

---

## 16. Recommended first commit

**D32.** Four lines, in a pure domain function, with executed before/after evidence, restoring an explicitly-intended behaviour that an earlier commit (`aed1e70`) already tried to establish — and it is currently corrupting the contact list a human picks up the phone and calls.

**Then R1 + R2**, because unlike everything else in this plan, they can break a routine `python boot.py` upgrade today.

---

## P5 / P6 outcomes

**D28's reachability question answered itself once the queue was removed.** The plan marked the pool's queue path "reachability UNKNOWN". It is reachable, and that was the problem: `release()` returned contexts to a queue and `acquire()` handed them back out, so a context built for one fingerprint was reused under another. The queue, `pool_size`, the recycle counters and the health-check task are all deleted; `acquire()` now always builds fresh. That also disposes of the reason D10 and D11 mattered so much, since a leaked context is only expensive if something else is holding contexts open beside it.

Deleting the bound needs saying out loud: nothing now limits how many contexts are live at once. That is exact rather than optimistic today, because the only production caller is `worker_scraper`, whose stream consumer processes one entry at a time. The pool docstring records where an `asyncio.Semaphore` belongs on the day a consumer fans out.

**D37 and D38 did not need the live Places capture the plan asked for.** Both were filed as HYPOTHESIS pending one. D37 is decidable from `from_api`'s own rules: they define what "unparseable" means, so counting parsed results where the API reports returned ones is wrong on the code's own terms. D38 is a decision about two results already in hand, not about what the API sends.

**D38's plan text calls `phone` "the dedup key". It is not** -- `_Accumulator` keys on `place_id`. `phone` is the *product* of the sweep, which is precisely why dropping it on the cross-pass merge matters: the second pass can carry the only phone number for a place the first pass found, and the merge threw it away.

**D23's header is `X-Request-ID`, not the `X-Correlation-ID` the plan named**, and there is no auth *middleware* to order the correlation middleware against -- API keys are checked by a FastAPI dependency, which runs after all middleware regardless. Ordering is still load-bearing: correlation is added last so it is outermost, because Starlette applies user middleware with the most recently added on the outside.

**D7's sweep found the shared client is not the common cause.** The audit implied one. Every `target_http` caller already branches on status, and `notifier.py` bypasses the wrapper entirely via `get_client()`, so the fix went to the two call sites that actually parsed a rejected response as if it were data.

**D7 has an unlisted sibling that made a P4 fix unreachable.** Both export plugins swallowed every delivery failure and returned success, which meant `worker_reporter`'s `DeliveryFailedError` path -- added in P4 -- could never be entered. Fixing D7's OpenRouter case alone would have left the reporter still reporting successful delivery of payloads that were rejected.

**D6 was decided in favour of deletion.** The plan's standing instruction was to delete unless a concrete shape could be verified live, and no live capture is obtainable in this environment. The regex found `APP_INITIALIZATION_STATE` every time and the walk parsed zero businesses out of it every time (observed live in P2: 35,267 bytes matched, 0 records), because the bootstrap blob is not shaped like `data[0][1][i][14]`. Six tests went with it: they exercised `_parse_search_results` against fixtures hand-built to the structure it expects, so they described the Go port rather than testing this code path. What remains is a behaviour control asserting Maps-shaped HTML yields no records -- it passed before the deletion and after, which is what makes the deletion safe -- plus a source-drift guard, because this fallback's failure mode was an empty result that reads as "no businesses at this location".

**D14 is closed without merging the transports, and that is the finding.** The plan asked for consolidation. Their signatures are the reason not to: `GuardedTransport` carries `allowed_private_hosts`, which `create_scoped_client` needs so an adapter can reach one private endpoint without weakening anything else, and `SSRFValidatingTransport` carries the `SSRF_EGRESS_ENFORCE` log-only opt-out. Merging means dropping one of those capabilities, and both have callers. Reading the pair side by side did surface two real defects, and those are fixed: `SSRFValidatingTransport` had **no scheme check at all** (a `gopher://` URL passed its gate and reached the inner transport, while `GuardedTransport` refused the same URL), and `GuardedTransport` **resolved DNS twice per request and per redirect hop**, so the policy check read one answer while the connection was pinned to another. One shared pre-DNS check, `require_supported_url()`, now gates both.

**D14's headline mechanism and D31 were both already fixed** in the working tree when P6 reached them -- the blocking resolvers are behind `asyncio.to_thread`, and `HttpClient.get_client()` no longer accepts `allow_private`. Controls now pin both rather than leaving them to be re-broken silently.

**One existing security test was rewritten, not deleted.** `test_guarded_transport_blocks_dns_rebinding` asserted D1's property *through the old implementation*: it needed a second, independent lookup to exist so the rebound answer could be caught on that second look. With one lookup there is no second answer, and the guarantee becomes structural -- resolve once, validate that answer, connect to that exact address. The rewritten test asserts the guarantee itself (exactly one resolution; the inner transport receives the validated IP with the original `Host` header), and a new sibling covers the other ordering, where the single answer is itself a metadata address.

**P5/P6 verification scope.** **1077 tests pass, 9 skipped, 0 failed** (1021 after P4; +56). An earlier full run of the same tree reported `1 failed` -- `tests/test_cli.py::test_health_reports_required_and_optional_checks`, whose subprocess `cli.py health` exceeded its 180s timeout on a loaded machine. `cmd_health` launches Playwright and pings Valkey directly and touches nothing P5 or P6 changed; it passed on the 494s rerun of the identical tree, against 1151s for the run that timed out. `mypy --strict` over its configured scope: clean. import-linter: 1 contract kept, 0 broken. ruff: **88 findings repo-wide**, the same baseline P4 recorded. Every defect guard was observed failing against unfixed source before its fix, and every control was observed passing both before and after. The same environment limits as P4 apply, and are stated in each test's docstring rather than implied to be more: no Docker, no live Postgres, no live Valkey, no live Chromium, no API keys. So D27's Postgres mirror is checked by reading plus `tests/integration/test_postgres_repos.py`, which needs a server; D17 runs on `fakeredis` rather than `AsyncMock`, deliberately, because `AsyncMock` cannot reproduce the `WRONGTYPE` error that is the whole defect; and D7 uses `httpx.MockTransport` rather than a hand-rolled fake for the same reason.

**RTK misreports pytest.** It reported the first full P6 run as "1070 passed, exit code 0" when the raw tee log for that same run said `1 failed, 1069 passed, 9 skipped`. Every tally in this section was read from unfiltered output (`rtk proxy python -m pytest`), never from the wrapper's summary.
