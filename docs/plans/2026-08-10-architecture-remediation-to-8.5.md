# Architecture Remediation Plan — Score 3.0 → ≥ 8.5

**Date:** 2026-08-10
**Branch at time of writing:** `fix/e2e-correctness-and-headless-cli`
**Source:** ARCH-AUDIT-V2 pass (2026-08-10) + security grounding pass
**Status:** In progress — W0, W1, W2, W3, W4, W6 complete. W5.1/5.2/5.3/5.4 complete (C12, F14, C8 closed); W5.5 (migration-tooling rewrite) deferred as flagged follow-up. W7, W8 not started. Note: this branch also carries substantial concurrent work from outside this plan (P0-P2/A1/A3/A4/SEC-1b — worker naming, job reaper, AutoThrottle, stealth profiles, SSRF egress) landing via separate commits during W5; not tracked in this document.

This document supersedes `ARCHITECTURE_REMEDIATION_PLAN.md`, `ARCHITECTURE_REMEDIATION_v2.md`,
and `ARCHITECTURE_REMEDIATION_v3.md`. Those are consolidated into ADRs by W8.

---

## Table of Contents

- [0. Exit Criteria](#0-exit-criteria)
- [1. Target Paradigm Per Module](#1-target-paradigm-per-module)
- [2. Finding Register](#2-finding-register)
- [3. Workstreams](#3-workstreams)
  - [W0 — Verification Gate](#w0--verification-gate)
  - [W1 — Security Hardening](#w1--security-hardening)
  - [W2 — Kill the Three Half-Migrations](#w2--kill-the-three-half-migrations)
  - [W3 — Reconnect Severed Paths](#w3--reconnect-severed-paths)
  - [W4 — Composition Root](#w4--composition-root)
  - [W5 — Persistence Consolidation](#w5--persistence-consolidation)
  - [W6 — Delete Aspirational Scaffolding](#w6--delete-aspirational-scaffolding)
  - [W7 — Observability & Scale](#w7--observability--scale)
  - [W8 — Documentation Consolidation](#w8--documentation-consolidation)
- [4. Sequencing](#4-sequencing)
- [5. Score Projection](#5-score-projection)
- [6. Risk Register](#6-risk-register)
- [7. Progress Log](#7-progress-log)

---

## 0. Exit Criteria

Rubric target: **8 = minor drift in 1–2 modules, no critical violations.** 10 adds
*consistent, observable, testable, scalable*. To clear 8.5, every gate below must hold.

| Gate | Condition | Verified by | Done |
|---|---|---|---|
| G1 | Zero CRITICAL, zero HIGH findings open | Re-run audit Phases 2/5 | [ ] |
| G2 | One queue mechanism, one pipeline, one entity model — no module marked `DEPRECATED` on the live path | `grep` proves zero importers of removed modules | [x] |
| G3 | Every advertised API endpoint has an observable effect on the live path | Integration test per endpoint | [ ] |
| G4 | Auth survives restart and works across replicas | Test: mint key on node A, use on node B, restart A, reuse | [ ] |
| G5 | Coverage ≥ 80% on `src/`, CI green, security scans clean | CI artifact | [ ] |
| G6 | Deployment manifests provision only what code consumes | `docker compose config` + boot smoke test | [ ] |
| G7 | Domain layer has zero infrastructure imports, enforced not conventional | `import-linter` contract in CI | [ ] |

---

## 1. Target Paradigm Per Module

Not one paradigm for the whole system. Chosen per layer by what that layer optimizes for.

| Module | Paradigm | Patterns | Rationale |
|---|---|---|---|
| `src/domain/` | Pure functional core. No I/O, no async, immutable | Value Object (`frozen=True` Pydantic), Algebraic State Machine (`JobState`), Protocol-as-Port | Testable without mocks. State machine is already correct — make immutability structural rather than documented |
| `src/application/` | Imperative shell / use-case interactors | Use Case per operation, Dependency Inversion (ports injected via constructor), Result type for expected failure | Orchestration is currently smeared across worker `__init__` methods; interactors make each flow independently testable |
| `src/extractors/` | Strategy + Chain of Responsibility | Strategy registry keyed by Protocol, ordered chain with explicit fallthrough | Already the right shape — needs deduplication (two chains exist today) and a registry instead of a dict literal |
| `src/infrastructure/queues/` | Adapter behind a single Port | `MessageBus` Protocol, one Streams adapter, Dead Letter Channel | Two implementations today. One Port makes the swap testable and permanently kills this drift class |
| `src/infrastructure/repositories/` | Repository + Unit of Work | UoW for transactional outbox, cursor pagination, optimistic concurrency (already present) | Outbox atomicity is currently broken — see F14 |
| `src/infrastructure/ai/` | Adapter + resilience decorators | `LLMProvider` Port, Circuit Breaker, Bulkhead (semaphore), Cache-Aside (already correct) | Provider is hardcoded to Gemini; `openai` is installed but unused. A Port makes multi-provider real or lets the dependency be dropped |
| `main.py` / API layer | Thin controllers + Composition Root | FastAPI `Depends()` DI, lifespan-scoped container, DTO separate from domain model | Module-level singletons today; one endpoint already deviates from even that |
| `src/security/` | Policy objects, fail-closed | Defense in depth (validate at every egress, not only at ingress), Decorator for enforcement | SSRF guard validates at submit time; the fetch happens in another process — see F13 |

---

## 2. Finding Register

Severity scale: CRITICAL blocks merge, HIGH should block merge, MEDIUM is a maintainability concern.

### From the ARCH-AUDIT-V2 compliance matrix

| ID | Finding | Severity | Evidence | Status |
|---|---|---|---|---|
| C1 | Overlay CANDIDATE→SHADOW→ACTIVE promotion has no effect on live extraction. The live processor uses `UniversalExtractionStrategy`, which never consults `OverlayRepository` | CRITICAL | `worker_processor.py:56-59`; `extraction_pipeline.py:132-144`; `shadow_evaluator.py:29` | ✅ Closed (W3.1) |
| C2 | `post_processor.run_state_audit()` filters `isinstance(entity, Opportunity)`, but the live strategy emits only `ExtractedRecord`. Every entity is silently discarded on every job; `status_counts` is always all-zero | CRITICAL | `post_processor.py:33-34`; `universal_strategy.py` (all three methods build `ExtractedRecord`) | ✅ Closed (W2.3) |
| C3 | Processor publishes discovery events to a Valkey LIST (`discovery_events_queue`); reporter consumes a Valkey Stream (`discovery_stream`). The two never meet | CRITICAL | `worker_processor.py:98`; `worker_reporter.py:82-89` | ✅ Closed (W2.1) |
| C4 | `valkey_worker.py` is marked DEPRECATED but is the only load-bearing queue. `stream_queue.py` (DLQ, retries, consumer groups) has no live producer | CRITICAL | `main.py:27,50`; `stream_queue.py:82` — sole caller is `outbox_relay.py:114` | ✅ Closed (W2.1) |
| C5 | `OutboxRelay.run_forever()` is never started anywhere. Outbox rows accumulate and are never relayed | CRITICAL | Exhaustive grep across `main.py`, `boot.py`, `worker_*.py`, `cli.py` — zero matches | ✅ Closed (W3.5) |
| C6 | `pipeline.py::DataPipeline` is marked DEPRECATED but is live; `DeterministicExtractionPipeline` is shadow-only | HIGH | `worker_processor.py:12,49`; `pipeline.py:4-10` | ✅ Closed (W2.2) |
| C7 | `ai_enrichment_enabled=True` is inert — `_enrich_opportunity()` returns immediately | HIGH | `pipeline.py:41-42, 102-104` | ✅ Closed (W2.2) — flag removed, not reimplemented |
| C8 | All five repositories hardcode `aiosqlite` and ignore `settings.database.url`. `docker-compose.enterprise.yml` sets `DB_URL` for a Postgres the app never reads | HIGH | `job_repository.py:62` and four siblings | ✅ Closed (W5.3) — see caveat: not yet verified against a live Postgres |
| C9 | No Kafka client code exists despite `aiokafka`, `KafkaSettings`, and Kafka/Zookeeper compose services | MEDIUM | `event_bus.py` and `saga_orchestrator.py` import only each other | Open — W6.2/W6.4 |
| C10 | OTel deps and `jaeger` service present; `observability_tracing.py` is imported only by the orphaned `saga_orchestrator.py` | MEDIUM | Grep importer list | Open — W6.3/W6.8 |
| C11 | `docker-compose.enterprise.yml` `scheduler` service runs `python scheduler.py`; no such file exists | HIGH | `docker-compose.enterprise.yml:280-296` | Open — W6.7 |
| C12 | `spacescraper_intel.db` and `spacescraper_jobs.db` hold overlapping "extracted entity lifecycle" concerns | MEDIUM | `sqlite_tracker.py:24` vs `job_repository.py:62` | ✅ Closed (W5.1) |
| C13 | `main.py` uses module-level singletons; `promote_overlay` deviates by constructing a fresh repo per request | LOW | `main.py:53-68` vs `main.py:450-452` | ✅ Closed (W4.3) |

### From the security grounding pass

| ID | Finding | Severity | Evidence | Status |
|---|---|---|---|---|
| F11 | `POST /auth/register` is unauthenticated. Any anonymous caller mints an `enterprise`-tier API key. Rate limiting is keyed per API key, so an attacker who can mint unlimited keys defeats all tier limits | CRITICAL | `main.py:232-249` — no `Depends(verify_api_key)` | ✅ Closed (W1.1) |
| F12 | API keys are stored in a process-local dict. Keys are destroyed on restart, and a key minted on one process is rejected by every other process. Authentication is non-functional above a single process — while `docker-compose.enterprise.yml:166` runs `--workers 4` | CRITICAL | `auth_middleware.py:94,150`; class docstring acknowledges the gap | ✅ Closed (W1.2) |
| F13 | SSRF guard is bypassable. `validate_outbound_url()` resolves DNS in the API process at submit time; the fetch happens later in `worker_scraper` against the raw URL through an httpx client with `follow_redirects=True` and no per-hop revalidation. Two working bypasses: DNS rebinding (documented in the code comment and never mitigated), and a public URL that 302-redirects to `169.254.169.254` | HIGH | `ssrf_guard.py:31-34`; `http_client.py:34`; `worker_scraper.py:317` | ✅ Closed (W1.3) — enforcement is opt-in (`SSRF_EGRESS_ENFORCE`), still defaulted to log-only |
| F14 | Transactional outbox is not transactional. `create_job()` and `create_outbox_event()` are two awaits on two separate `aiosqlite` connections. A failure between them loses the event or orphans it | HIGH | `main.py:305` then `main.py:308` | ✅ Closed (W3.6/3.7) |
| F15 | `/autograph` payload guard is dead code. `sanitize_for_prompt()` truncates to 2000 chars before `validate_payload_size(max_bytes=512_000)` runs, so the size check can never fire. Side effect: `compact_html_for_prompt(max_chars=6000)` never sees more than 2000 chars | MEDIUM | `main.py:261-263`; `input_sanitizer.py:33`; `ai/client.py:143` | ✅ Closed (W1.4) |
| F16 | `/health` and `/slo` return hardcoded `sample_metrics`. A health endpoint reporting a constant 0.92 success rate will report healthy during a total outage | HIGH | `main.py:214-221`, `main.py:495-502` | ✅ Closed (W3.8/3.9) |

### Verified clean

- `.env` is gitignored and untracked; `.env.example` contains placeholders only.
- API keys are SHA-256 hashed at rest; `secrets.token_urlsafe(32)` is correct CSPRNG usage.
- No raw SQL string interpolation in the repository adapters read.
- `src/domain/ports.py` has zero infrastructure imports.
- Valkey is used correctly for ephemeral state only (TTL'd dedup keys, fan-out counters, AI cache) — never as source of truth.

---

## 3. Workstreams

---

### W0 — Verification Gate

**Size:** S · **Blocks:** everything · **Risk:** none (additive only)

No CI exists. Every subsequent deletion is unsafe without a green test gate.

- [x] **W0.1** Add `.github/workflows/ci.yml` running `pytest` on push and PR
- [x] **W0.2** Add coverage reporting; record the current baseline as the initial floor — 60% (`src/`, 307 passed, 1 xfailed); CI floor set to 58% (small buffer)
- [x] **W0.3** Add `ruff` lint job — 845 pre-existing violations found; 859 auto-fixed (safe, mechanical); 63 remaining scoped to a documented `ignore` list in `pyproject.toml` for later triage
- [x] **W0.4** Add `mypy` job — `--strict` on `src/domain/` only; 4 pre-existing `type-arg`/`assignment` errors fixed (bare `dict` → `dict[str, Any]`, implicit-Optional)
- [x] **W0.5** Add `bandit -r src/` security lint — 1 real finding fixed (MD5 `usedforsecurity=False` in `image_downloader.py`); 3 SQL-fstring findings verified as false positives (fixed literal fragments, parameterized values) and suppressed inline with justification
- [x] **W0.6** Add `pip-audit` dependency vulnerability scan
- [x] **W0.7** Add secret scanning (`gitleaks`) to prevent `.env`-class regressions
- [x] **W0.8** Add `import-linter` contract: `src.domain` may not import `src.application` or `src.infrastructure` (satisfies G7) — required adding `__init__.py` to every `src/` package dir (was an implicit namespace package tree); verified clean (1 kept, 0 broken)
- [x] **W0.9** Write `tests/test_post_processor.py` — the absence of this file is why C2 went undetected. 5 real tests + 1 `xfail(strict=True)` that documents C2 precisely and will flip to a hard failure (forcing a fix) once W2.3 retypes `run_state_audit()` against `ExtractedRecord`
- [x] **W0.10** Ratchet the coverage floor toward 80% as later workstreams add tests — floor mechanism in place (`--cov-fail-under=58`); actual ratcheting happens as W1–W7 land tests

**Exit:** CI green on `master` and on this branch. Coverage floor enforced.

---

### W1 — Security Hardening

**Size:** M · **Depends:** W0 · **Do before any deploy**

#### W1.1 — Close the open key-minting endpoint (F11) — ✅ done

- [x] Remove anonymous access to `POST /auth/register`. Chose: gate behind an admin-tier key via a new `verify_admin_key` dependency (`ADMIN_API_KEY` env var, constant-time compare, fails closed if unset) — the CLI mint path (`ApiKeyGenerator`) remains available as the other option
- [x] Add per-IP rate limiting on the registration path — `check_registration_rate_limit()`, `REGISTRATION_IP_LIMIT = 5`/day, reuses the same Valkey/local-fallback counter as tier limits
- [x] Restrict self-selectable tiers — satisfied by the admin gate itself (registration is no longer anonymous/self-service, so "self-selectable" no longer applies); left as-is rather than adding a second, redundant tier check
- [x] Test: anonymous `POST /auth/register` returns 401, not 200 — `tests/integration/test_api_smoke.py::test_anonymous_registration_is_rejected` + `test_registration_with_wrong_admin_key_is_rejected` + `test_registration_is_rate_limited_per_ip`

#### W1.2 — Persist API keys (F12) — ✅ done

- [x] Add `ApiKeyRepository` Protocol to `src/domain/ports.py` (`create_key`, `get_by_hash`, `get_by_key_id`, `set_active`)
- [x] Add SQLite adapter (`src/infrastructure/repositories/api_key_repository.py`) following the existing repository pattern. Columns: `key_hash`, `key_id`, `tier`, `owner_email`, `created_at`, `expires_at`, `is_active`
- [x] Change `validate_key()` to look up by hash from the repository
- [x] Ship behind a dual-read flag (memory + repository) — writes go to both; reads check the repository first, fall back to memory on a miss or a repository error
- [x] Add a revocation path — `ApiKeyManager.revoke_key(key_id)` + repository `set_active()`, exposed via `python auth_middleware.py revoke <key_id>` (CLI, symmetric with mint)
- [x] Verify expiry enforcement now has data to act on — unchanged check in `verify_api_key`, now backed by real persisted `expires_at`
- [x] Test (satisfies G4): mint key on node A → use on node B → restart A → key still valid — `tests/test_auth_middleware.py::test_key_survives_restart_and_is_visible_on_another_node`
- Side effect: `ApiKey`/`ApiTier` moved from `auth_middleware.py` into `src/domain/models.py` (value objects belong in the domain layer per Section 1; `auth_middleware.py` re-exports both for backward compatibility). `generate_api_key()` is now async — updated the two callers (`main.py`, `integration_example.py`) and the CLI entrypoint.

#### W1.3 — Enforce SSRF at egress, not only at submit (F13) — ✅ done

- [x] Implement a validating httpx transport (`src/security/validating_transport.py::SSRFValidatingTransport`) that re-resolves the hostname and re-checks the IP inside the connection attempt, pinning the connection to the validated IP (SNI/Host carry the original hostname)
- [x] Re-validate every redirect hop instead of trusting `follow_redirects=True` — httpx invokes the transport per hop; test: `test_redirect_hop_is_revalidated`
- [x] Apply the transport to the shared `http_client` singleton so every outbound call inherits it
- [x] Add an explicit deny-list for cloud metadata endpoints by name (`169.254.169.254`, `metadata.google.internal`, `metadata.goog`) in addition to the existing CIDR checks — shared constants in `ssrf_guard.py`, used by both the submit-time check and the transport
- [x] Keep the submit-time check as a fast-fail for user feedback, but stop treating it as the security boundary — docstring updated to say so explicitly
- [x] Ship in log-only mode first to measure over-blocking, then enforce — `SSRF_EGRESS_ENFORCE` env var, **defaults to log-only (unset = observe, not block)**. **Follow-up not yet done: flip to enforce in each deployment once log-only output has been reviewed for false positives — nothing in this plan does that automatically.**
- [x] Test: URL that 302-redirects to a private IP is blocked at fetch time in enforce mode, logged (not blocked) in log-only mode — `tests/test_validating_transport.py` (6 tests)

#### W1.4 — Fix the `/autograph` guard ordering (F15) — ✅ done

- [x] Validate payload size against the raw input, before sanitizing and truncating — reordered in `main.py`
- [x] Reconcile the three inconsistent limits — resolved by removing the sanitizer's truncation entirely (that was never its job): raw input ≤ 512 KB (hard reject) → injection-filtered (no size change) → compacted to ≤ 6000 chars (the real prompt budget, `compact_html_for_prompt`, already correctly wired). The 2000-char cap is gone, not reconciled to a third number.
- [x] Test: oversize payload is rejected with 400 rather than silently truncated — `test_sanitize_for_prompt.py` truncation test replaced with `test_prompt_not_truncated`; existing `validate_payload_size` tests cover the reject path

#### W1.5 — Bound AI concurrency — ✅ done

- [x] Add an `asyncio.Semaphore` bulkhead around `_call_gemini_api` — `AI_MAX_CONCURRENCY` env var, default 10. Test: `test_call_gemini_api_bounds_concurrency`

**Exit:** F11, F12, F13, F15 closed. Security scans green in CI (325 passed, 1 xfailed; ruff/mypy/bandit/import-linter clean on all changed files).

---

### W2 — Kill the Three Half-Migrations

**Size:** L · **Depends:** W0 · **Score-dominant workstream** · **Highest risk**

Root cause of all five CRITICAL audit findings. Same failure mode three times: new
implementation built, old one never removed, neither fully wired.

**Method — strictly additive, then subtractive.** Fix and prove the surviving path live
(W3) before deleting anything. Grep-verify zero importers immediately before each deletion.

#### W2.1 — One queue (C4) — ✅ done

- [x] Decide: Streams. It already has consumer groups, DLQ, retry, dedup, and idle-message claiming. The LIST path has none of these. (ADR write-up deferred to W8.2 — decision is implemented, not yet documented)
- [x] Define a `MessageBus` Protocol in `src/domain/ports.py` (`connect`, `close`, `push`, `push_dlq`, `consume`, `get_allowed_fanout`, `get_stream_length`, `get_dlq_length`, `get_pending_count`)
- [x] Make `ValkeyStreamQueue` the sole adapter for it — repointed every producer/consumer: `main.py`, `worker_scraper.py`, `worker_processor.py`, `worker_reporter.py`, `cli.py`, `submit_url.py`, `spacescraper.py`. Added a shared `make_message()` envelope helper in `stream_queue.py` so every producer builds the same shape
- [x] Port the memory-backpressure guard from `valkey_worker.py:75-95` into `ValkeyStreamQueue.push()` — now guards every stream, not just jobs (all streams share the same Valkey memory budget)
- [x] Port the atomic Lua fan-out counter from `valkey_worker.py:182-215` into `ValkeyStreamQueue.get_allowed_fanout()`
- [x] Repoint `main.py` job submission to the Streams adapter
- [x] Remove the now-dead LIST poller loops from `worker_scraper.py` and `worker_processor.py` (`asyncio.gather` dual-consume collapsed to a single `stream_queue.consume()` call each)
- [x] Grep-verify zero importers, then delete `src/infrastructure/queues/valkey_worker.py`
- **Bonus fix, discovered while wiring this through:** C3 (processor→LIST, reporter→Streams, never connected) is closed as a direct consequence — both sides now share one stream (`discovery_stream`) and one envelope shape. `worker_reporter.py`'s consumer previously expected a payload shape (`{"aggregate_id": ..., "data": {...}}`) that no producer ever sent — rewritten to deserialize the `DiscoveryEvent` directly
- **Also fixed in the same pass (bugs found while making the stream path live, since it had never actually run before):** `process_stream_message` in both `worker_scraper.py` and `worker_processor.py` was silently dropping fields (`persona_id`, `correlation_id`, `max_depth`, `overlay`, `webhook_url`) when reconstructing `ScrapeJob`/`RawScrapePayload` from the envelope — now reconstructs from the full payload dict instead of hand-picking fields
- **Not done, correctly out of scope:** `stealth_brain.py` also imported `ValkeyQueueWorker` purely as a raw Valkey client handle (no queue semantics used) — given its own minimal Valkey connection instead of a dependency on the queue class

#### W2.2 — One pipeline (C6) — ✅ done

- [x] Keep `DeterministicExtractionPipeline`. It honors `OverlayRepository`, has schema validation, and is the documented successor. (ADR write-up deferred to W8.3)
- [x] Extract the fuzzy dedup clustering from `pipeline.py:162-244` into a separate `Deduplicator` collaborator (`src/application/deduplicator.py`) rather than folding it into the pipeline — keeps the pipeline single-responsibility. Retargeted from the procurement-specific `Opportunity` model (buyer/deadline/external_id) to generic `ExtractedRecord` (canonical_url exact match + fuzzy title match within same `record_type`)
- [x] Reconcile the two overlay dict shapes into one schema — resolved by deleting `universal_strategy.py` entirely (its `container`/`mapping` shape was its only content); `container_selector`/`field_mappings` (`extraction_pipeline.py`) is now the only shape that exists
- [x] **Update the AI prompt in the same commit.** `ai/client.py::generate_overlay` now requests `container_selector`/`field_mappings`, matching the surviving pipeline
- [x] Add a round-trip test: `generate_overlay()` output feeds the surviving pipeline successfully — `test_extraction_pipeline.py::test_ai_generated_overlay_shape_round_trips_through_pipeline`
- [x] Grep-verify zero importers, then delete `src/application/pipeline.py` and `src/extractors/universal_strategy.py` (the whole file, not just the chain — it had no other content)
- [x] Remove `ai_enrichment_enabled` (C7) — not reimplemented. The flag gated a stub (`_enrich_opportunity` returned immediately, see original C7 finding), so removing it changes no live behavior; implementing it for real would be net-new feature work outside this workstream's scope
- **New orchestrator, since `DeterministicExtractionPipeline` is itself a `BaseExtractionStrategy` (a strategy, not an end-to-end pipeline with hashing/dedup):** `ExtractionPipeline` in `extraction_pipeline.py` replaces `DataPipeline`, keeping the exact same `.process(payload, strategy) -> ProcessingResult` contract so `worker_processor.py`'s call site needed only its two constructor args swapped. Guarantees both `identity_hash` and `content_hash` are set regardless of which extraction stage built the record (previously `extraction_pipeline.py`'s builders set only `identity_hash` and `universal_strategy.py`'s only `content_hash` — inconsistent, though not a live bug since `post_processor.py` already falls back between the two)
- **Callers updated to match:** `worker_processor.py`, `cli.py::_extract_from_html` (used by `cli.py extract`/`scrape`/`health`), `demo_run.py`
- **Discovered, not fixed (correctly out of scope):** no strategy on the live path — old or new — ever constructs a `FollowLink`. Recursive discovery (`ProcessingResult.follow_urls`) has been dead code since before this plan; `DataPipeline` always returned `[]` here too. Not a W2.2 regression, not fixed here — implementing discovery is net-new feature work
- **Test fallout:** `tests/test_extractors_generic.py` deleted (tested the now-deleted `UniversalExtractionStrategy`, fully superseded by `test_extraction_pipeline.py`'s coverage of `DeterministicExtractionPipeline`); `tests/test_resilience_identity_hash.py` split — the 3 `DataPipeline`-dependent tests replaced with 2 equivalent tests against `ExtractedRecord.compute_identity_hash()`, the 3 `post_processor`-focused tests kept as-is (untouched by this workstream, `Opportunity` still exists until W2.3); `tests/test_ai_cache_and_client.py`'s example overlay shapes updated for consistency (cache-behavior tests, not schema-sensitive, so this was cosmetic, not required for correctness)
- **One real regression caught by the test suite, fixed:** `cli.py`'s `health` command's extraction self-probe used a 2-word article body; `DeterministicExtractionPipeline`'s semantic-HTML stage requires >50 chars of body text (stricter than the deleted `UniversalExtractionStrategy`, which had no such minimum). Fixed by lengthening the probe fixture — this is `cli.py` checking itself with unrealistic input, not an extraction-pipeline bug

#### W2.3 — One entity model (C2) — ✅ done

- [x] `ExtractedRecord` wins. Its own docstring states it replaces the others. (ADR write-up deferred to W8.4)
- [x] Remove `Product`, `Lead`, `Article`, `Opportunity` from the `ProcessingResult.entities` type union — now `list[ExtractedRecord | FollowLink | dict[str, Any]]`. `FollowLink` kept (undeprecated, discovery-metadata type — not one of the four removed); `BaseEntity` kept (`FollowLink` still extends it)
- [x] Retype `post_processor.run_state_audit()` against `ExtractedRecord` — `isinstance(entity, Opportunity)` → `isinstance(entity, ExtractedRecord)`; identity key changed from `entity.url` to `entity.canonical_url or entity.source_url` (the same key `Deduplicator` and `SqliteTracker` use)
- [x] Retype `sqlite_tracker` against `ExtractedRecord` — the `opportunities` table (20 Opportunity-shaped columns: source, external_id, buyer, country, budget, embedding, classification, ...) replaced with a generic `records` table (id, record_type, canonical_url, source_url, `data` JSON blob, identity_hash, content_hash, first_seen, last_seen, change_type, data_classification); `get_opportunity_by_id`/`upsert_opportunity` → `get_record_by_id`/`upsert_record`. Four Opportunity-specific methods with zero live callers (`find_similar_opportunities`, `get_opportunity_by_external_id`, `get_recent_opportunities`, `upsert_opportunities_batch`) deleted rather than speculatively retyped — nothing calls them and their query shape (title/buyer/source filters) doesn't generalize to a free-form `data` dict
- [x] Retype `DiscoveryEvent.entities` — `list[Opportunity]` → `list[ExtractedRecord]`
- [x] Delete the deprecated entity classes once no importers remain — `Product`, `Lead`, `Article`, `Opportunity` deleted from `domain/models.py`

**Real importers found and fixed** (beyond the checklist's named files — grep-verified with `\b(Product|Lead|Article|Opportunity)\b` across every `.py` file before and after deletion):
- `src/infrastructure/exports/base_plugin.py` + `plugins.py` (`WebhookExportPlugin`, `SlackExportPlugin`) — **live code**, reachable from `worker_reporter.py` whenever `SLACK_WEBHOOK_URL` is configured. First grep pass missed this directory entirely; caught by `tests/test_module_imports.py` (a genuine save — this is exactly what that test exists for) rather than by the manual grep. Retyped to `ExtractedRecord`; `SlackExportPlugin` rewritten to read `t.data.get("title"/"buyer"/"estimated_budget")` instead of Opportunity's typed attributes
- `src/infrastructure/exports/report_generator.py` — type hint only (`generate_excel_csv` was already generic at runtime via `.model_dump()`)
- `tests/conftest.py` + `tests/integration/test_processor_audit.py` — shared `sample_opportunity` fixture → `sample_record`, now builds `ExtractedRecord`
- `scripts/dry_run_reporter.py` — dev script, mock `Opportunity` instances → mock `ExtractedRecord`
- `tests/test_resilience_fanout_cap.py` — dead `Opportunity`/`FollowLink` imports removed

**Deleted as dead weight, not retyped** (all zero-importer once `integration_example.py` was trimmed, all deeply Opportunity-shaped in a way that doesn't generalize — redesigning them for a free-form `data` dict would be net-new feature work, not a retype):
- `src/postgres_tracker.py` — zero importers anywhere; SQLAlchemy mirror of the old `sqlite_tracker.py`, same C8/W5.3 orphaned-Postgres story
- `src/application/llm_enrichment.py` — zero importers anywhere
- `src/data_quality.py` — only importer was `integration_example.py` (a standalone demo script); its `demo_data_quality()` function and the now-dead imports removed from that script, the other two demos (auth, caching) kept

**Test fallout:** `tests/test_post_processor.py` fully rewritten against `ExtractedRecord` (`FakeIntelTracker` now implements `get_record_by_id`/`upsert_record`) — **the W0.9 `xfail` is gone**, replaced with `test_extracted_record_entities_are_audited_not_silently_dropped`, asserted unconditionally: this is the direct regression test for C2's closure. `tests/test_resilience_identity_hash.py`'s remaining 3 `post_processor` tests retyped from `Opportunity` to `ExtractedRecord` (their mocked tracker method names had to change too, since `MagicMock` doesn't fail on a renamed method — it silently returns a fresh `MagicMock` for whatever's called, which would have made these tests pass for the wrong reason if left un-migrated)

**Full suite: 316 passed, 0 failed, 0 xfailed** (down from 319 — net effect of deleting `test_extractors_generic.py`-equivalent dead-code tests and consolidating fixtures nets out to fewer, not-redundant tests; the interesting number is 0 xfailed, was 1). ruff (`src/`)/mypy/bandit/import-linter all clean.

**Exit:** C2, C4, C6, C7 closed. No `DEPRECATED` module on the live path — verified via `grep -rn "DEPRECATED\|deprecated"`, the only hits left are the unrelated `REDIS_URL`→`VALKEY_URL` settings alias (satisfies G2).

---

### W3 — Reconnect Severed Paths

**Size:** M · **Depends:** W2

- [x] **W3.1** (C1) Wire the surviving pipeline's `OverlayRepository` lookup into the processor's live path so promotion has real effect — `worker_processor.py` constructs a real `SqliteOverlayRepository`, initializes/closes it in `run()`'s lifecycle, and passes it into `DeterministicExtractionPipeline(overlay_repo=...)` (previously `overlay_repo=None`)
- [x] **W3.2** (C1) Integration test: promote an overlay to ACTIVE → assert the next extraction on that domain uses it — `tests/integration/test_overlay_wiring.py` (2 tests: ACTIVE overlay is used; CANDIDATE overlay is not)
- [x] **W3.3** (C2 + C3) Verify discovery events now flow — both breaks are fixed by W2.1 (one bus) and W2.3 (one model). Confirmed: `ReportGenerator`/`artifact_writers.py` were already `ExtractedRecord`-native (built during W2.3's blast radius, no separate fix needed here)
- [x] **W3.4** (C3) End-to-end test: job → scraper → processor → reporter, assert an artifact is written — extended the existing `tests/integration/test_cluster_e2e.py::test_cluster_processes_job_end_to_end` past record persistence through `discovery_stream` → reporter → real artifact file (redirected to `tmp_path`, not the repo's `exports/` dir). Caught a real test-setup gap while writing this: the e2e test bypasses `ProcessorWorkerService.run()` (drives `process_stream_message` directly), so the newly-added `overlay_repo.initialize()` in `run()` never fired — fixed by initializing it explicitly in the test, matching the pattern already used for the other swapped-in repos
- [x] **W3.5** (C5) Start `OutboxRelay.run_forever()` as a lifespan task — `main.py` now constructs `outbox_relay = OutboxRelay(outbox_repo, stream_queue=stream_queue)` (shares the app's existing Valkey connection instead of opening a second one) and the lifespan starts/cancels it alongside `strategy_selector`'s existing background task
- [x] **W3.6** (F14) Wrap `create_job` + `create_outbox_event` in a Unit of Work sharing one connection and one transaction — `SqliteJobRepository.create_job` and `SqliteOutboxRepository.create_event` both gained optional `commit`/`conn` kwargs (default-safe, only call site is `main.py`'s `/jobs` handler); the handler now writes both rows on `job_repo._conn` without committing, commits once, and rolls back + re-raises on any failure
- [x] **W3.7** (F14) Test: simulated failure between the two writes leaves no orphaned job and no orphaned event — `tests/integration/test_api_smoke.py::test_job_submission_rolls_back_on_outbox_write_failure` monkeypatches `outbox_repo.create_event` to raise, confirms the job table is empty afterward
- [x] **W3.8** (F16) Wire `/health` and `/slo` to real `metrics_tracker` values; delete the hardcoded `sample_metrics` dicts — added `metrics_tracker.initialize()`/`close()` to the lifespan (previously never connected in the API process) and a shared `_current_slo_metrics()` helper that reports only the fields metrics_tracker actually tracks (`extraction_success_rate` from `get_success_rate()`, `block_rate` from `captcha_encountered / jobs_total` when jobs exist) rather than fabricating the other four SLO fields
- [x] **W3.9** (F16) Test: degraded metrics produce a degraded health response — `test_health_reflects_real_degraded_metrics` records one failed job via `metrics_tracker.record_job_status(False)` and confirms `/health` flips from `healthy` to `degraded` with an `extraction_success_rate` alert

**Exit:** C1, C3, C5, F14, F16 closed. Every advertised endpoint has an observable effect (satisfies G3).

---

### W4 — Composition Root

**Size:** M · **Depends:** W3

- [ ] **W4.1** Build a lifespan-scoped container in `main.py`
- [ ] **W4.2** Expose repositories and the message bus via FastAPI `Depends()` instead of module-level singletons (`main.py:53-68`)
- [x] **W4.1** Built `AppContainer` (`main.py`) — a plain `@dataclass` composition root holding every repository + the message bus (`stream_queue`, `job_repo`, `record_repo`, `outbox_repo`, `overlay_repo`, `obs_repo`, `strategy_selector`, `outbox_relay`), constructed once via `AppContainer.build()`. Lifespan startup/teardown now loop over `container.repos()` instead of 5 repeated `await x.initialize()`/`await x.close()` pairs.
- [x] **W4.2** Every endpoint that touches a repo or the queue (`/jobs` POST/GET/cancel/records, `/records/{id}/feedback`, `/overlays/{id}/promote`) now takes it via `Depends(get_*)` instead of closing over a bare module global. `metrics_tracker`/`slo_monitor`/`ai_orchestrator`/`api_key_manager` deliberately left as direct imports — cross-cutting singletons with no per-request override need in any current or planned test, wrapping them would be unrequested abstraction.
- [x] **W4.3** Fix the per-request `SqliteOverlayRepository()` deviation in `promote_overlay` (`main.py:450-452`) — C13. `promote_overlay` now uses the container's `overlay_repo` singleton via `Depends(get_overlay_repo)`, matching every other endpoint. Bonus fix while touching this function: removed a phantom `OverlayState.CANARY` reference (never a real enum member — only `CANDIDATE, SHADOW, ACTIVE, DISABLED, RETIRED` exist) that crashed the transition-path validation with `AttributeError` on both real transition paths (CANDIDATE→SHADOW, SHADOW→ACTIVE). Pre-existing total breakage, uncaught because no test exercised this endpoint before `test_overlay_promotion_uses_shared_repo_connection` was added this session.
- [x] **W4.4** `ProcessorWorkerService` gained optional constructor params for `job_repo`, `record_repo`, `overlay_repo`, `intel_tracker` (each self-constructs when omitted, same optional-injection pattern `stream_queue` already used). `ScraperWorkerService` gained `obs_repo`. `ReporterWorkerService` gained `stream_queue` (previously zero injection points, hardcoded `ValkeyStreamQueue()`) plus the `_owns_stream_queue` close-guard the other two workers already had. `tests/integration/test_cluster_e2e.py`'s processor wiring simplified from construct-then-overwrite-4-attributes-then-manually-rebuild-the-strategy-dict to passing everything at construction — the manual strategy-dict rebuild (needed because the old code swapped `overlay_repo` out from under an already-built `DeterministicExtractionPipeline`) is gone entirely.
- [x] **W4.5** `tests/integration/test_api_smoke.py`'s one genuine monkeypatch — `main.outbox_repo.create_event = failing_create_event` — replaced with `main.app.dependency_overrides[main.get_outbox_repo] = lambda: _FailingOutboxRepo()`, a real injected fake instead of a runtime attribute patch on the shared singleton. The other `main.X` references in that file were direct reads/writes to shared state for test setup/assertion (seeding an overlay, checking pending-event counts) — not monkeypatching — so they were left as `main.container.X`, not converted to overrides.

**Exit:** ✅ C13 closed. Endpoints unit-testable without patching module state — verified: the one place a test needed to inject failure behavior now does so via `app.dependency_overrides`, not an attribute patch.

---

### W5 — Persistence Consolidation

**Size:** M · **Depends:** W2.3

- [x] **W5.1** (C12) Collapse `spacescraper_intel.db` into `spacescraper_jobs.db`. `SqliteTracker`'s default `db_path` now points at `spacescraper_jobs.db`; its table renamed `records` → `intel_records` to avoid colliding with `record_repository.py`'s own `records` table now sharing the file. Bonus cleanup: deleted the `runs` table and `log_run()` method — zero callers anywhere in the codebase, dead since before this plan started. Scope note: this consolidates the *file* (one store, per the Exit criterion) but deliberately does not merge the two overlapping `records`-shaped tables into one — `record_repo`'s table is an append-only per-job audit log (many rows per URL over time, keyed by `record_id`+`job_id`) while `intel_tracker`'s is a deduped latest-state-per-URL table for change detection (upserted, keyed by URL) — genuinely different access patterns over similar-shaped data, and merging them would mean rewriting `IntelligencePostProcessor`'s tested contract (`tests/test_post_processor.py`) for a MEDIUM finding whose Exit criterion is about store count, not schema unification. Flagging this the same way W4's full-DI-rewrite scope was flagged, rather than silently doing the bigger rewrite or silently stopping short.
- [x] **W5.2** (F14) Introduce Unit of Work spanning repositories — already closed via W3.6/W3.7 (job_repo + outbox_repo now share one connection/transaction). No separate work needed here.
- [x] **W5.3** (C8) Decide Postgres. **User chose implement** (surfaced via AskUserQuestion — the delete option was recommended given zero live callers on `DatabaseSettings`/`database_models.py`, but the user picked the bigger option). Five new adapters — `PostgresJobRepository`, `PostgresRecordRepository`, `PostgresOutboxRepository`, `PostgresOverlayRepository`, `PostgresObservationRepository` (`src/infrastructure/repositories/postgres_*_repository.py`) — implement the same `src.domain.ports` Protocols the SQLite adapters do, using `asyncpg` directly (not `database_models.py`'s SQLAlchemy ORM, which is stale Opportunity-era schema and stays unused/dead — a separate cleanup, not done here). A second fork question (single connection vs. real `asyncpg.Pool`) went to the user too: **single connection chosen** — matches the SQLite adapters' existing shape exactly (`main.py`'s F14 unit-of-work reaches into `job_repo._conn` directly), zero call-site changes, same concurrency ceiling the SQLite backend already has. `PostgresConnection` (`postgres_conn.py`) wraps a bare `asyncpg.Connection` to reproduce aiosqlite's hold-transaction-open-until-commit behavior so F14's guarantee holds under Postgres too. `src/infrastructure/repositories/factory.py` picks the backend via an explicit `PERSISTENCE_BACKEND` env var (default `sqlite`) — not DSN-presence sniffing, since `DatabaseSettings.url` always has a default value even when `DB_URL` isn't set. `src/bootstrap.py`'s `AppContainer` (the single composition root from W4, now shared by `main.py` and both worker entrypoints per a later, independent commit) builds repos via the factory and types its fields against the ports Protocols instead of concrete `Sqlite*` classes. `docker-compose.enterprise.yml`'s 7 app containers now set `PERSISTENCE_BACKEND=postgres` alongside their existing `DB_URL` — closing the actual complaint (DB_URL nothing read). **Bonus bug fix, found while hand-translating `create_evaluation`:** `SqliteObservationRepository.create_evaluation()` had 13 SQL placeholders for 14 bound values — raised `sqlite3.ProgrammingError` on every real call (`ShadowEvaluator.evaluate_candidate()` calls it live), zero test coverage, never caught. Fixed at the root in both the SQLite original and the new Postgres sibling; regression test added (`tests/test_observation_repository.py`). **Not verified against a real server**: Docker Desktop's daemon wasn't running locally, so the new repos are syntax-checked and covered by skip-gated integration tests (`tests/integration/test_postgres_repos.py`, `TEST_POSTGRES_URL`-gated) plus a new CI job (`postgres-repos` in `ci.yml`, spins up a real `postgres:16-alpine` service) — but that CI job has not actually run yet (GitHub Actions billing is currently blocking all CI on this repo, unrelated to this change). Treat as implemented-but-unverified until that job goes green.
- [x] **W5.4** Recorded as `docs/adr/0001-postgres-backend.md`.
- [ ] **W5.5** Migration path (`migrate_sqlite_to_postgres.py`, `verify_migration.py`) — **deferred, flagged not silently skipped.** Both files (584 + 165 lines) are written against the pre-W2.3 `Opportunity`-era schema (`opportunities`, `runs` tables) and don't touch any of the 10 tables the current domain model actually uses. This isn't a patch, it's a rewrite comparable in size to W5.3 itself. Scoped as separate follow-up work, not attempted in this pass.

**Exit:** C8, C12 closed (W5.5 — the migration-tooling rewrite — remains open as flagged follow-up). One store, one transaction boundary.

---

### W6 — Delete Aspirational Scaffolding

**Size:** S · **Depends:** W0 · **Highest score-per-effort — start alongside W0**

Every item below is verified zero-live-importer. Deleting is pure drift removal.

- [x] **W6.1** (C9) Delete `src/saga_orchestrator.py` — zero importers, deleted
- [x] **W6.2** (C9) Delete `src/event_bus.py` — only importer was `saga_orchestrator.py`, deleted alongside it
- [x] **W6.3** (C10) Delete `src/observability_tracing.py` — only importer was `saga_orchestrator.py`, deleted alongside it
- [x] **W6.4** (C9) Delete `KafkaSettings` from `config_settings.py`, the `aiokafka` dependency, and the `kafka` / `zookeeper` / `kafka-ui` services from `docker-compose.enterprise.yml`
- [x] **W6.5** Delete the `openai` dependency — zero live importers, removed from both requirements files
- [x] **W6.6** Delete the `apscheduler` dependency — zero live importers, removed
- [x] **W6.7** (C11) Delete the `scheduler` service from `docker-compose.enterprise.yml` — no `scheduler.py` exists, service removed rather than writing net-new code
- [x] **W6.8** (C10) Delete the OTel dependencies and the `jaeger` service — `ObservabilitySettings` also removed from `config_settings.py` (only consumer was the now-deleted `observability_tracing.py`)
- [x] **W6.9** Regenerate `docker-compose.enterprise.yml` from what the code actually consumes — removed Kafka/Zookeeper/Kafka-UI, Jaeger, scheduler services and all now-dead env vars (`KAFKA_BOOTSTRAP_SERVERS`, `OTEL_*`) from every service definition; also dropped the obsolete top-level `version:` key (compose warns it's ignored)
- [x] **W6.10** (G6) Add a CI smoke test that boots the enterprise manifest and hits `/health` — new `enterprise-manifest-smoke` job in `ci.yml`: `docker compose config` validates every service reference resolves (would have caught C11 pre-merge), then boots `postgres` + `valkey` + `api-gateway` and polls `/health` before tearing down

**Exit:** C9, C10, C11 closed. Manifests match code (satisfies G6). **Not yet verified**: the new CI job hasn't run — GitHub Actions billing is currently blocking all CI on this repo (same block affecting W5.3's `postgres-repos` job), unrelated to this change.

---

### W7 — Observability & Scale

**Size:** M · **Depends:** W1, W4 · **Takes the score from 8 to 9**

- [ ] **W7.1** Define an `LLMProvider` Port; make Gemini an adapter behind it
- [ ] **W7.2** Move circuit-breaker state to Valkey so it is shared across replicas. Today `--workers 4` produces four independent, mutually-ignorant circuit breakers
- [ ] **W7.3** Move the embedding LRU from a process-local class attribute (`ai/client.py:237`) to Valkey, reusing the already-correct two-level `AICache` design
- [ ] **W7.4** Wire OTel tracing for real, or delete it via W6.8. Correlation IDs already propagate end-to-end, so tracing is a small increment
- [ ] **W7.5** Expose a Prometheus scrape endpoint matching the `config/prometheus.yml` that already exists
- [ ] **W7.6** Load test: confirm the AI path is bounded and the orchestrator is horizontally scalable

**Exit:** Orchestrator state is shared, not per-process. Observable under load.

---

### W8 — Documentation Consolidation

**Size:** S · **Depends:** W2, W5

Eight overlapping audit and remediation markdown files sit in the repo root. The absence
of ADRs is why these half-migrations kept recurring — no record of what was decided, so
each attempt restarted from scratch.

- [ ] **W8.1** Create `docs/adr/` with a standard ADR template
- [ ] **W8.2** ADR: queue mechanism (Streams over LIST) — from W2.1
- [ ] **W8.3** ADR: extraction pipeline (`DeterministicExtractionPipeline`) — from W2.2
- [ ] **W8.4** ADR: entity model (`ExtractedRecord`) — from W2.3
- [ ] **W8.5** ADR: Postgres yes/no — from W5.3
- [ ] **W8.6** ADR: Kafka yes/no — from W6.4
- [ ] **W8.7** ADR: API key storage and registration policy — from W1.1, W1.2
- [ ] **W8.8** Collapse `architecture_audit.md`, `architecture_audit_v7.md`, `ARCHITECTURE_REMEDIATION_PLAN.md`, `ARCHITECTURE_REMEDIATION_v2.md`, `ARCHITECTURE_REMEDIATION_v3.md`, `audit_increment2.md`, `audit_increment2_final.md`, and `implementation_audit_report.md` into one `docs/ARCHITECTURE.md` plus the ADRs above
- [ ] **W8.9** Delete the superseded root-level markdown files

**Exit:** One architecture document, one ADR per decision.

---

## 4. Sequencing

```
W0 (CI gate) ──┬─→ W1 (security) ─────────────────────────────────┐
               │                                                   ├─→ W7 (observability/scale)
               ├─→ W2 (kill half-migrations) ─→ W3 (reconnect) ─→ W4 (DI) ─┤
               │                                     │                     │
               ├─→ W6 (delete scaffolding) ──────────┼─────────────────────┤
               │                                     │                     │
               └─────────────────────────────────────┴─→ W5 (persistence) ─┘
                                                                            │
                                                                            └─→ W8 (ADRs)
```

**Parallelizable:** W1 and W6 are independent of W2/W3. W6 is the cheapest score-per-effort
item — start it at the same time as W0.

**Non-negotiable ordering:**

1. W0 before any deletion. Without a green test gate, every `rm` is a gamble.
2. W2 before W3. Reconnecting a path that is about to be replaced is wasted work.
3. W3 before W4. Do not refactor dependency injection around a broken flow.

---

## 5. Score Projection

| After | Open CRITICAL | Open HIGH | Projected score | Driver |
|---|---|---|---|---|
| Today | 5 | 5 | **3.0** | Three silent half-migrations plus two auth holes |
| W0 + W1 | 3 | 3 | 4.5 | Security closed; correctness still broken |
| + W2 | 1 | 1 | 6.5 | One queue, one pipeline, one model; overlay still unwired |
| + W3 | 0 | 0 | **8.0** | All advertised features have real effect. Clears "no critical violations" |
| + W4 + W5 + W6 | 0 | 0 | **8.8** | Consistent patterns, manifests match code, ≤2 modules with minor drift |
| + W7 + W8 | 0 | 0 | **9.3** | Observable, scalable, decisions recorded |

**Reaching ≥ 8.5 requires completing through W6.** W0–W3 alone stops at roughly 8.0 —
consistency and deployment-truth gaps hold it there.

---

## 6. Risk Register

| Step | Risk | Mitigation | Accepted |
|---|---|---|---|
| W2 deletions | Removing a module with an undiscovered caller | Grep-verify zero importers immediately before each deletion; CI gate from W0 must be green | [ ] |
| W2.2 overlay schema reconcile | AI prompt emits the old shape → `/autograph` output silently unusable by the surviving pipeline | Update the prompt in the same commit; add a round-trip test | [ ] |
| W1.2 auth persistence | Cutover invalidates live keys | Dual-read (memory + repository) for one release, then drop the memory path | [ ] |
| W1.3 egress validation | Over-blocking legitimate scrape targets | Explicit metadata deny-list; ship in log-only mode first, then enforce | [ ] |
| W5.3 Postgres | Large surface, low urgency | Ship last. SQLite is adequate until concurrency is the measured bottleneck | [ ] |
| W6 deletions | Deleting something a future roadmap item needs | Record the decision as an ADR first (W8) so the reversal is cheap and documented | [ ] |

---

## 7. Progress Log

| Date | Workstream | Change | Score estimate |
|---|---|---|---|
| 2026-08-10 | — | Plan created from ARCH-AUDIT-V2 + security grounding pass | 3.0 (baseline) |
| 2026-08-13 | W0 | CI gate landed: `.github/workflows/ci.yml` (test+coverage, ruff, mypy --strict/domain, bandit, pip-audit, gitleaks, import-linter); `tests/test_post_processor.py` written (documents C2 via `xfail`); baseline coverage 60%; 1 real bandit finding (MD5) fixed; 3 bandit SQL findings confirmed false-positive and suppressed with justification; 4 mypy strict errors fixed in `src/domain/`; `src/` converted from implicit namespace packages to regular packages for import-linter | 3.0 → not yet re-scored (W0 is additive-only; no CRITICAL/HIGH findings closed yet) |
| 2026-08-13 | W1 | All 5 sub-items closed: admin-gated + per-IP-rate-limited `/auth/register` (F11); SQLite-backed `ApiKeyRepository` with dual-read/write and restart/cross-node durability (F12); `SSRFValidatingTransport` re-validating every request and redirect hop at connect time, log-only by default (F13); `/autograph` payload-size check reordered before sanitization, sanitizer's silent truncation removed (F15); `asyncio.Semaphore` bulkhead on Gemini calls. `ApiKey`/`ApiTier` relocated from `auth_middleware.py` to `src/domain/models.py` as the plan's Section 1 paradigm calls for. 3 new test files + 1 extended; full suite 325 passed / 1 xfailed; ruff/mypy/bandit/import-linter clean | 3.0 → 4.5 per Section 5 projection (F14, F16, and all C-series findings remain open — G1 not yet satisfied) |
| 2026-08-13 | W2.1 | One queue: Streams is now the sole mechanism. `MessageBus` Protocol added to `ports.py`; every producer/consumer across `main.py`, both workers, the reporter, and every CLI/manual-submit script repointed; memory-backpressure guard and Lua fan-out counter ported into `ValkeyStreamQueue`; `valkey_worker.py` deleted (zero importers verified first). C4 closed. C3 closed as a direct consequence — the processor→LIST/reporter→Streams mismatch no longer exists once both sides share one stream. Two additional dormant bugs surfaced and fixed while making the Streams path live for the first time: both workers' stream-message reconstruction was silently dropping several `ScrapeJob`/`RawScrapePayload` fields. `stealth_brain.py` decoupled from the queue class (it only ever needed a raw Valkey handle). 5 test files rewritten/added; full suite 323 passed / 1 xfailed (net -2 tests: 1 superseded LIST-backend test deleted, discrepancy vs. prior 325 not fully reconciled — see note below); ruff (`src/` — the actual CI scope)/mypy/bandit/import-linter all clean. Root-level files (`main.py`, `cli.py`, `worker_*.py`, `spacescraper.py`) are outside CI's `ruff check src/` scope; safe auto-fixes applied to every file this changeset touched, one pre-existing bug found but left alone (`spacescraper.py` F811 — `Colors` class shadowed by a later import — pre-existing, zero test coverage, out of W2 scope) | 4.5 → ~5.2 (one CRITICAL — C4 — plus a bonus CRITICAL — C3 — closed; C1/C2/C5 still open so G1 not yet satisfied) |
| 2026-08-13 | W2.2 | One pipeline: `DeterministicExtractionPipeline` is now the sole extraction strategy. New `ExtractionPipeline` orchestrator (`extraction_pipeline.py`) replaces `DataPipeline` behind the same `.process()` contract; new `Deduplicator` collaborator (`deduplicator.py`) ports the fuzzy-dedup logic from `Opportunity` onto generic `ExtractedRecord`. AI overlay prompt (`ai/client.py::generate_overlay`) updated to the surviving `container_selector`/`field_mappings` shape in the same commit, with a round-trip test guarding the two from drifting apart again. `pipeline.py` and `universal_strategy.py` deleted (zero importers verified). C6, C7 closed. Callers updated: `worker_processor.py`, `cli.py`, `demo_run.py`. One real regression caught by the suite and fixed: `cli.py health`'s extraction self-probe was too short for the new pipeline's stricter (>50 char) semantic-HTML gate. Full suite 319 passed / 1 xfailed; ruff (`src/`)/mypy/bandit/import-linter all clean | ~5.2 → ~5.8 (two more HIGH findings closed; C1, C2, C5 and all F14/F16 still open) |
| 2026-08-14 | W2.3 | One entity model: `ExtractedRecord` is now the sole live entity type. `Product`, `Lead`, `Article`, `Opportunity` deleted from `domain/models.py`; `ProcessingResult.entities` and `DiscoveryEvent.entities` retyped. `post_processor.py` and `sqlite_tracker.py` retyped (the latter's schema replaced wholesale — `opportunities` table with 20 procurement-specific columns → generic `records` table with a `data` JSON blob; 4 zero-caller Opportunity-specific tracker methods deleted rather than speculatively generalized). C2 closed — **this is the finding the whole plan started from**: every extracted entity was being silently discarded on every job with no log, exception, or metric. Manual grep for importers missed `src/infrastructure/exports/base_plugin.py`+`plugins.py` entirely (live Slack/webhook export code, reachable from `worker_reporter.py`) — caught instead by `tests/test_module_imports.py`, which is exactly the kind of regression that test exists to catch; fixed and retyped properly. `postgres_tracker.py`, `llm_enrichment.py`, `data_quality.py` deleted (zero real callers, all Opportunity-shaped in ways that don't generalize to a free-form record). W0.9's `xfail` test is gone, replaced with an unconditional pass — the direct regression test for this fix. Full suite 316 passed, 0 failed, 0 xfailed; all four CI gates clean. **G2 satisfied**: one queue, one pipeline, one entity model, zero `DEPRECATED` modules on the live path | ~5.8 → ~6.5 (matches the plan's own Section 5 projection point "+ W2 → 6.5"; W2 fully closed) |
| 2026-08-14 | W3.1–3.4 | Overlay wiring + discovery-event e2e. `worker_processor.py` now injects a real `SqliteOverlayRepository` into `DeterministicExtractionPipeline` (was `overlay_repo=None`) — C1 closed, promoting an overlay to ACTIVE has real effect on live extraction, covered by 2 new integration tests. Extended the existing cluster e2e test through the full `discovery_stream` → reporter → artifact-file path (previously stopped at record persistence), confirming C2+C3's fixes hold end-to-end; caught and fixed one test-setup gap (overlay_repo needs explicit init when a test drives `process_stream_message` directly instead of `run()`). Full suite: 317 passed, 1 deselected (`test_cli.py`'s browser-launching health check — verified separately to pass; excluded only because Playwright's Chromium launch was taking multiple minutes under heavy machine load this session, unrelated to any code change), 0 failed | ~6.5 → ~7.0 (C1 closed; C5, F14, F16 remain — W3.5–3.9 not yet started) |
| 2026-08-14 | W3.5–3.9 | Reconnected the last three severed paths. **C5:** `main.py` now constructs `outbox_relay = OutboxRelay(outbox_repo, stream_queue=stream_queue)` (reuses the app's existing Valkey connection) and the lifespan starts/cancels its `run_forever()` task alongside `strategy_selector`'s. **F14:** `create_job`/`create_event` gained optional `commit`/`conn` kwargs (default-safe, single call site); `/jobs` now writes both rows on one connection inside one transaction, rolling back and re-raising on failure instead of orphaning a job with no outbox event. **F16:** added `metrics_tracker.initialize()`/`close()` to the lifespan (it was never connected in the API process, so `/health`/`/slo` were reading a permanently-empty local cache even before the hardcoded dict); replaced both endpoints' `sample_metrics` literals with a shared `_current_slo_metrics()` helper that reports only the fields metrics_tracker actually has data for (`extraction_success_rate`, `block_rate`) rather than fabricating the other four SLO fields — `slo_monitor.evaluate()` already skips absent metrics gracefully, so this is honest silence, not a regression in coverage. 3 new tests, each a direct regression test for its finding: outbox delivery actually happens, a simulated mid-transaction failure leaves zero rows in either table, and a real failed-job recording flips `/health` from `healthy` to `degraded`. Full suite: 320 passed, 1 deselected (same Playwright health check, same reason), 0 failed; ruff (`src/`)/mypy/bandit/import-linter all clean. **W3 fully closed** | ~7.0 → ~7.5 (C5, F14, F16 closed — every CRITICAL finding in the register is now closed; remaining open findings are all MEDIUM/LOW/HIGH-but-deferred, scoped to W4–W6) |
| 2026-08-14 | W4.3 | Closed C13, the plan's last LOW finding tied to a concrete endpoint. `promote_overlay` now uses the module-level `overlay_repo` singleton instead of constructing/initializing/closing its own `SqliteOverlayRepository` per request. New regression test (`test_overlay_promotion_uses_shared_repo_connection`) surfaced a pre-existing, total-breakage bug while exercising this endpoint for the first time: `promote_overlay`'s transition-path validation referenced `OverlayState.CANARY`, which was never a real enum member — `OverlayState` only defines `CANDIDATE, SHADOW, ACTIVE, DISABLED, RETIRED`. This crashed both real transition paths (CANDIDATE→SHADOW, SHADOW→ACTIVE) with `AttributeError` on every call, uncaught because no test had ever exercised the endpoint before. Fixed at the root (removed the CANARY branches and the stale `PromoteRequest.target_state` field description) rather than worked around. **Scope call, flagged not silently applied:** W4's other four sub-items (W4.1 lifespan DI container, W4.2 `Depends()`-based injection, W4.4 worker constructor injection, W4.5 replace test monkeypatching) are a full composition-root rewrite justified only by this one LOW finding plus general hygiene — disproportionate scope for what the finding register requires, so left undone pending user direction rather than executed or silently dropped. Full suite: 12/12 `test_api_smoke.py` passed; ruff (`src/`)/mypy --strict (`src/domain/`)/bandit/import-linter all clean | ~7.5 → ~7.6 (C13 closed — every CRITICAL and every concretely-scoped finding in the register is now closed; remaining open items are the W4 DI-rewrite question plus W5/W6/W7/W8) |
| 2026-08-14 | W4.1/4.2/4.4/4.5 | User opted into the full composition-root rewrite over the W4.3-only scope call above. `AppContainer` (plain `@dataclass`, `main.py`) is now the single place every repo + the message bus is constructed and wired; lifespan init/close collapsed from 5 repeated pairs to a loop over `container.repos()`. All 7 repo/queue-touching endpoints converted from bare-global reads to `Depends(get_*)` providers backed by the container — `metrics_tracker`/`slo_monitor`/`ai_orchestrator`/`api_key_manager` deliberately left as direct imports (no test or endpoint needs a per-request override for them; wrapping them would be unrequested abstraction, not part of what W4.2 asked for). All three workers gained the constructor-injection points W4.4 called for (`ProcessorWorkerService`: `job_repo`/`record_repo`/`overlay_repo`/`intel_tracker`; `ScraperWorkerService`: `obs_repo`; `ReporterWorkerService`: `stream_queue`, previously zero injection points at all), each optional and self-constructing when omitted — same pattern `stream_queue` already used elsewhere, extended rather than reinvented. `test_cluster_e2e.py`'s processor wiring simplified from construct-then-overwrite-4-attributes-then-manually-rebuild-the-strategy-dict (the old code swapped `overlay_repo` out from under an already-built `DeterministicExtractionPipeline`, so the strategy had to be rebuilt by hand afterward) to passing everything at construction — that rebuild step is gone. W4.5's one genuine monkeypatch (`main.outbox_repo.create_event = failing_create_event`, a runtime attribute patch on the shared singleton) replaced with `app.dependency_overrides[main.get_outbox_repo] = lambda: _FailingOutboxRepo()`, an actual injected fake; the file's other `main.X` references were legitimate shared-state reads/writes for test setup and assertion, not monkeypatching, so left as `main.container.X` rather than forced through overrides they don't need. Full suite: 321 passed, 1 deselected (same pre-existing Playwright health-check exclusion), 0 failed; ruff (`src/`)/mypy --strict (`src/domain/`)/bandit/import-linter all clean. **W4 fully closed** | ~7.6 → ~7.8 (composition root in place; remaining open items are W5 Persistence Consolidation, W6 Delete Aspirational Scaffolding, W7 Observability & Scale, W8 Documentation Consolidation) |
| 2026-08-22 | W5.1/5.2 | **C12:** `SqliteTracker` (the `intel_tracker` used only for change-detection inside `IntelligencePostProcessor`) defaulted to its own `spacescraper_intel.db` file, holding a `records` table nearly identical in shape to `record_repository.py`'s own `records` table in `spacescraper_jobs.db` — same file-count sprawl this plan's Exit criterion targets. Repointed its default `db_path` to `spacescraper_jobs.db` and renamed its table `records` → `intel_records` to avoid colliding with the repo's own table now sharing the file. Bonus: deleted the `runs` table and `log_run()` method — zero callers anywhere in the codebase. Deliberately did *not* merge the two `records`-shaped tables into one: `record_repo`'s is an append-only per-job audit log (`record_id`+`job_id` keyed, many rows per URL over time) while `intel_tracker`'s is a deduped latest-state-per-URL table (URL-keyed, upserted) — a real behavioral difference, and merging them would mean rewriting `IntelligencePostProcessor`'s tested contract for a MEDIUM finding whose stated Exit is store count, not schema unification. **W5.2 (F14 Unit of Work):** already closed via W3.6/W3.7 in the prior session — no new work needed, checkbox was just never flipped. 15/15 tests passed across `test_post_processor.py`/`test_processor_audit.py`/`test_resilience_identity_hash.py`/`test_cluster_e2e.py`; ruff/mypy --strict/import-linter clean. Environment note: this session's fresh checkout was missing the `scrapling` package the prior session's parser migration (P7.1) added to `requirements.txt` — installed it to unblock collection, unrelated to W5. Also noted mid-session: `worker_scraper.py`/`engine.py`/`observation_repository.py`/`models.py` plus new `src/domain/throttle.py` carry uncommitted AutoThrottle work-in-progress from outside this conversation — left untouched, not part of W5 | ~7.8 → ~7.85 (C12 closed; F14 already was. C8 — the Postgres implement-vs-delete decision, W5.3/5.4/5.5 — surfaced to the user rather than picked unilaterally, see below) |
| 2026-08-22 | W5.3/5.4 | **C8, user chose implement over delete** (both options surfaced via AskUserQuestion; delete was recommended given zero live callers on `DatabaseSettings`/`database_models.py`, but the user picked the bigger option — a second fork on connection model followed, single-connection chosen over a real `asyncpg.Pool`, see ADR). Five new adapters (`src/infrastructure/repositories/postgres_{job,record,outbox,overlay,observation}_repository.py`) implement the same `src.domain.ports` Protocols as their SQLite siblings, hand-translated query-by-query (`?` → `$n`, `cursor.rowcount`-based conflict detection → `RETURNING` + `fetchrow`) rather than routed through `database_models.py`'s SQLAlchemy ORM, which is stale Opportunity-era schema (`opportunities`/`runs`/`dead_letters`/`event_logs`) that predates W2.3's entity-model unification and doesn't match any current table — it stays unused, a separate future cleanup. `PostgresConnection` (`postgres_conn.py`) wraps a bare `asyncpg.Connection` to reproduce aiosqlite's implicit-transaction-until-commit behavior on `commit=False` calls, so F14's rollback guarantee (`main.py`'s job+outbox unit of work) holds under Postgres too — this was the one piece that needed real design, not just mechanical translation. `src/infrastructure/repositories/factory.py` selects the backend via an explicit `PERSISTENCE_BACKEND` env var (`sqlite` default), not by sniffing `DB_URL`'s presence (`DatabaseSettings.url` always has a default value). `src/bootstrap.py`'s `AppContainer` — extracted from `main.py` into its own module and extended with a `job_reaper` field by unrelated concurrent commits during this same window (`705316c feat(bootstrap): P0 gate closure`) — now builds its five repo fields via the factory and types them against the ports Protocols instead of concrete `Sqlite*` classes; `main.py`'s `Depends()` providers and endpoint signatures updated to match. `docker-compose.enterprise.yml`'s 7 app containers gained `PERSISTENCE_BACKEND=postgres` next to their existing `DB_URL` line, closing the literal C8 complaint. **Bonus fix, found while hand-translating `create_evaluation`:** `SqliteObservationRepository.create_evaluation()` had 13 SQL placeholders for 14 bound values — `sqlite3.ProgrammingError` on every real call, live-called by `ShadowEvaluator.evaluate_candidate()`, zero test coverage, never caught. Fixed at the root in both the SQLite original and the Postgres sibling; `tests/test_observation_repository.py` added as the regression test. **W5.4:** decision + design recorded as `docs/adr/0001-postgres-backend.md`. **Verification caveat:** Docker Desktop's daemon wasn't running locally, so the new repos were never exercised against a real Postgres server this session — only syntax-checked, plus a new skip-gated integration suite (`tests/integration/test_postgres_repos.py`, gated on `TEST_POSTGRES_URL`) and a new CI job (`postgres-repos` in `ci.yml`, real `postgres:16-alpine` service) that has not yet run (GitHub Actions billing is currently blocking all CI on this repo — unrelated pre-existing account issue, flagged separately). Existing suite: 472 passed, 5 initially failed in `test_resilience_turbo_guard.py` then passed clean on rerun — traced to the concurrent AutoThrottle work-in-progress in `worker_scraper.py` being mid-edit at the exact moment the suite ran, not caused by this change (confirmed: neither the failing file nor `worker_scraper.py` was touched here, and both isolated and full-file reruns are clean); ruff (`src/` + `main.py`)/mypy --strict/import-linter clean. **W5.5** (rewriting `migrate_sqlite_to_postgres.py`/`verify_migration.py`, both Opportunity-era, 584+165 lines) explicitly deferred as flagged follow-up — comparable in size to W5.3 itself, not attempted this pass | ~7.85 → ~7.95 (C8 closed pending real-Postgres CI verification; W5 otherwise complete except the flagged W5.5 migration-tooling rewrite) |
