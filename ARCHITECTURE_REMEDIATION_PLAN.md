# Spacescraper — Architecture Remediation Plan

**Goal:** Raise the architecture score from **5/10** to **>9/10**.
**Baseline audit:** ARCH-AUDIT-V2 (this repo). Score 5/10, Maturity: Early Production, Refactor Urgency: Immediate.
**Owner:** TBD
**Status:** Draft — not yet started.

---

## 0. Scoring contract (how we know we hit >9)

The audit rubric is non-negotiable. To score 9–10 we must demonstrably reach:

> *All layers correctly separated, patterns consistent, observable, testable, scalable.*

Concretely, **every** statement below must be TRUE and provable (test, log, or diff):

- [ ] No CRITICAL or HIGH violations remain open from Phase 2 / Phase 5.
- [ ] Auth validates real, stored keys. No prefix-bypass path reachable in any non-dev environment.
- [ ] Every user-supplied URL/HTML passes through SSRF + sanitizer before use.
- [ ] Persistence is accessed only through a `OpportunityRepository` port. Zero direct adapter imports in application/worker code.
- [ ] Application layer has zero imports from `src.infrastructure.*` concrete singletons (DI only).
- [ ] Domain entities are not mutated in-place mid-pipeline; enrichment is applied atomically via a result object.
- [ ] Inter-process payloads are fully Pydantic-validated end-to-end (no attribute injection).
- [ ] No orphaned parallel architectures or unreferenced root scripts in the tree.
- [ ] Test coverage ≥ 80% on `src/`, including unit + integration on the core data path.
- [ ] Observability: every job carries a correlation ID from API → scraper → processor → reporter, visible in logs/traces.

Each phase below lists its **Exit Criteria** mapped to these checkboxes.

---

## 1. Guiding principles for the refactor

1. **No behavioral regression.** Each step is behind a passing test before the next begins. Apply the project TDD rule (RED → GREEN → REFACTOR).
2. **Dependency rule is law.** `Interfaces → Infrastructure → Application → Domain`. Domain imports nothing outward. Application depends on Ports (Protocols), never concrete adapters.
3. **Ports before adapters.** Define the abstract Protocol, make the existing concrete class implement it, inject it. Swap is then free.
4. **Feature flags already exist** (`config_settings.features`). Use them for parallel/shadow rollout, not for permanently-dark code.
5. **Delete dead code.** Orphaned modules lower the score (Phase 5 anti-patterns). Removal is part of the work, not optional cleanup.

---

## PHASE A — Security CRITICAL/HIGH (Immediate)

Closes the three production-blocking violations. This phase alone moves the security posture from "breached" to "defensible" and removes all CRITICAL findings.

### A1. Real API-key validation (CRITICAL)
**Finding:** `auth_middleware.py:130–143` accepts any `ss_`-prefixed token as PRO tier. `main.py:228` never persists generated keys.

**Action:**
- Add a `ApiKeyStore` port (Protocol): `save(api_key: ApiKey)`, `get_by_hash(key_hash: str) -> ApiKey | None`, `revoke(key_id: str)`.
- Implement `RedisApiKeyStore` (HASH `apikey:{key_hash}` → serialized `ApiKey`) — Redis is already a hard dependency.
- `generate_api_key()` writes through the store; `/auth/register` (`main.py:224`) awaits the save.
- Rewrite `validate_key()` to hash the presented key and look it up. Remove the `startswith("ss_")` acceptance path entirely.
- Demo key (`ss_demo_key`) remains gated by `settings.environment == "development"` only.

**Exit criteria:** Auth test suite proves: unknown `ss_` key → 401; registered key → its real tier; revoked key → 403. No code path returns a synthetic PRO key. ☑ checkbox "Auth validates real, stored keys".

### A2. Wire SSRF guard + input sanitizer (HIGH)
**Finding:** `ssrf_guard.py` and `input_sanitizer.py` are tested but never called from `main.py`.

**Action:**
- `POST /jobs` (`main.py:265`): call `validate_outbound_url(str(submission.url))`; if `webhook_url` present, `validate_outbound_url(submission.webhook_url, require_https=(env=='production'))`. Map `SSRFGuardError` → HTTP 400.
- `POST /autograph` (`main.py:243`): `validate_payload_size(request.html_sample)` then `sanitize_for_prompt(...)` before passing to AI.
- Add the same `validate_outbound_url` call inside the HTTP client used by the scraper turbo path (`http_client.get`) so recursive/discovered URLs are also checked (defense against the DNS-rebind note already in `ssrf_guard.py`).

**Exit criteria:** Integration test posts a job with `http://169.254.169.254/...` and a `file://` webhook → both rejected 400. ☑ "Every user-supplied URL/HTML passes through SSRF + sanitizer".

### A3. CORS hardening (HIGH)
**Finding:** `main.py:105–111` — `allow_origins=["*"]` + `allow_credentials=True`.

**Action:**
- Add `SecuritySettings` to `config_settings.py`: `allowed_origins: list[str]` (default `[]`), `cors_allow_credentials: bool`.
- Drive `CORSMiddleware` from config. If origins is `["*"]`, force `allow_credentials=False`.

**Exit criteria:** Config-driven CORS; no wildcard+credentials combination possible. ☑ removes HIGH CORS finding.

**Phase A result:** All CRITICAL + 3 HIGH security findings closed. Score impact: unblocks everything; 5 → ~6.5.

---

## PHASE B — Dependency inversion & layer separation

Removes the layer leaks that cap the score at "moderate drift". Targets the rubric's "all layers correctly separated".

### B1. `OpportunityRepository` port (HIGH — dual persistence)
**Finding:** `SqliteTracker` active, `database_models.py`/`postgres_tracker.py` orphaned, no shared interface; `worker_processor.py:14` imports the adapter directly.

**Action:**
- Define `src/domain/ports/repository.py`: `OpportunityRepository` Protocol — `upsert(t: Opportunity) -> str`, `get_by_url(url) -> Opportunity | None`, `get_by_identity_hash(h) -> Opportunity | None`, `list_high_quality(min_score, limit)`.
- `SqliteTracker` and a new `PostgresOpportunityRepository` (built on existing `OpportunityModel`) both implement it.
- `IntelligencePostProcessor` and `worker_processor` receive the repository via constructor injection. A small factory (`build_repository(settings)`) picks the impl from `features.postgres_db`.

**Exit criteria:** Grep shows zero `import ... sqlite_tracker` / `postgres_tracker` in `application/` or worker modules — only the factory references concretes. `/opportunities/high-quality` (`main.py:368`) returns real rows. ☑ "Persistence accessed only through port".

### B2. AI orchestrator behind a port (HIGH — `pipeline.py:16`)
**Finding:** Application `pipeline.py` imports the concrete `ai_orchestrator` singleton.

**Action:**
- Define `EnrichmentProvider` Protocol in `src/domain/ports/`: `enrich(opportunity_dict) -> EnrichmentResult | None`, `embed(text) -> list[float] | None`, `generate_overlay(html) -> dict | None`, plus `enabled: bool`.
- `ai_orchestrator` implements it. Inject into `DataPipeline.__init__`. No module-level import of the singleton inside the pipeline.

**Exit criteria:** `DataPipeline` constructed with a mock provider in unit tests — no network, no singleton. ☑ "Application layer has zero infra singleton imports".

### B3. Move `embedding` out of the domain entity (LOW→removes boundary leak)
**Finding:** `Opportunity.embedding` (`models.py:119`) is an ML/infra artifact in the domain.

**Action:**
- Remove `embedding` from `Opportunity`. Carry it in an application-layer `EnrichedOpportunity` / `EnrichmentResult` (see C1) and in the persistence row only.
- Dedup (`pipeline._is_similar`) reads the embedding from the enrichment-side structure, not the domain entity.

**Exit criteria:** `Opportunity` contains only business fields. Dedup tests still pass.

**Phase B result:** Layer leaks closed; persistence + AI both swappable. Score impact ~6.5 → ~7.5.

---

## PHASE C — Pipeline correctness & immutability

Eliminates the temporal-coupling and in-place-mutation anti-patterns (Phase 5 #6) and the silent-failure modes (Phase 4 failure semantics).

### C1. Atomic enrichment via result object (MEDIUM)
**Finding:** `pipeline._enrich_opportunity` mutates `Opportunity` in place; hash order enforced only by comment (`pipeline.py:60–76`).

**Action:**
- `_enrich_opportunity` returns an immutable `EnrichmentResult` (frozen dataclass): `title_en, buyer_en, summary, normalized_budget_eur, embedding`.
- Pipeline computes `identity_hash` on the raw entity, then produces a **new** `Opportunity` via `model_copy(update=...)` applying enrichment, then computes `content_hash`. No in-place attribute writes.
- Make `Opportunity` config `frozen` where feasible, or enforce copy-on-write by convention + test.

**Exit criteria:** Reordering enrichment vs. identity-hash in a test causes a test failure (ordering now enforced structurally). ☑ "Domain entities not mutated in-place".

### C2. Explicit degradation logging (Phase 4)
**Action:**
- When `provider.enabled` is false or `enrich`/`embed` returns `None`, log a structured WARNING with `job_id` + degradation reason ("ai_disabled", "enrich_timeout", "embed_failed"). Increment a metric (`ai_degraded_total`).
- Dedup logs when it falls back from cosine to fuzzy because embeddings are absent.

**Exit criteria:** Forcing the provider off yields visible degradation logs + metric, not silent passthrough.

### C3. Shared embedding cache + bounded AI concurrency (Phase 4 scalability)
**Finding:** `_embedding_cache` is per-instance (`pipeline.py:39`); per-opportunity sequential `await` with no concurrency cap.

**Action:**
- Back the embedding cache with Redis (`emb:{sha1(text)}` → JSON vector, TTL from `AISettings.embedding_cache_size` policy). Keep a small L1 in-process LRU in front.
- Bound concurrent provider calls with an `asyncio.Semaphore` sized from config; enrich a batch with `asyncio.gather` under the semaphore instead of a serial loop.

**Exit criteria:** Two processor instances reuse cached embeddings (integration test with shared fakeredis). Concurrency cap proven by a test that counts in-flight calls.

**Phase C result:** Pipeline deterministic, observable, concurrent-safe. Score impact ~7.5 → ~8.

---

## PHASE D — Distributed-state & contract correctness

Targets the shared-mutable-state and payload-contract findings (Phase 3) that block "scalable".

### D1. Typed inter-process payload contract (MEDIUM)
**Finding:** `worker_scraper.py:112–113` injects `overlay`/`webhook_url` onto `RawScrapePayload` by attribute — bypasses Pydantic.

**Action:**
- Add `overlay: Optional[Dict[str,Any]] = None`, `webhook_url: Optional[str] = None`, `persona_id: Optional[str] = None` to `RawScrapePayload` in `domain/models.py`.
- Remove all dynamic attribute assignment; construct the payload with these fields.

**Exit criteria:** No `payload.<attr> = ...` injection remains (grep). Round-trip serialize/deserialize test asserts all fields survive. ☑ "Inter-process payloads fully validated".

### D2. Shared adaptive state for turbo/miss counts (Phase 3)
**Finding:** `_turbo_miss_counts` and `hybrid_domains` are per-process; diverge across `replicas: 2`.

**Action:**
- Move hybrid-domain registry and miss counters into Redis (`turbo:domains` SET, `turbo:miss:{domain}` counter with TTL). All scraper replicas read/write the shared state.

**Exit criteria:** Promotion on replica A is visible to replica B (integration test against shared fakeredis).

### D3. Bounded recursive job IDs (Phase 3)
**Finding:** `worker_processor.py:91` `job_id=f"rec_{payload.job_id}"` grows unbounded; root extraction is O(depth).

**Action:**
- Generate fresh `job_id=f"rec_{uuid4().hex[:8]}"`; store `root_job_id` in payload metadata and propagate it explicitly. Fan-out key uses `root_job_id` directly.

**Exit criteria:** Recursive jobs have bounded-length IDs; fan-out cap test (`test_resilience_fanout_cap`) still green.

**Phase D result:** Cluster state coherent under horizontal scaling. Score impact ~8 → ~8.5.

---

## PHASE E — Observability, dead-code removal, pattern consistency

Pushes from "good" to "9+": consistency, testability, observability across the whole tree.

### E1. End-to-end correlation IDs
**Finding:** `correlation.py` middleware + saga correlation exist but are not threaded through the queue hops.

**Action:**
- Stamp a `correlation_id` at `POST /jobs`, carry it on `ScrapeJob` → `RawScrapePayload` → `DiscoveryEvent`. Bind it into the logging context in every worker (`logger_config`). Emit on OTel spans (`observability_tracing`).

**Exit criteria:** A single job's correlation ID appears in API, scraper, processor, reporter logs. ☑ "Every job carries a correlation ID".

### E2. Decide Saga/EventBus: wire or remove (Phase 5 #3)
**Finding:** Saga `_persist_state`/`_extract_entities` are `pass`; EventBus not initialized in lifespan.

**Action (choose, don't leave dark):**
- **Wire:** Rebuild `worker_processor.process_payload` as saga steps (scrape→extract→classify→persist→notify) with real compensation backed by the `OpportunityRepository`; implement `_persist_state`. Initialize `event_bus` in `main.py` lifespan.
- **OR Remove:** If event-sourcing is out of near-term scope, delete `saga_orchestrator.py` + `event_bus.py` and the dead factories. (Premature abstraction is itself a scored anti-pattern.)
- Recommendation: **wire** the saga around the existing persistence path (high value, the steps already map to current stages); **defer Kafka** behind its flag but initialize EventBus.

**Exit criteria:** No method body is a bare `pass` placeholder in an active pattern. Either path removes Phase 5 #3.

### E3. Purge orphaned code (Phase 5 #4, #5)
**Action:**
- Delete or relocate the 5 root `dashboard*.py` into `src/interfaces/web/` keeping at most one canonical implementation.
- Remove `extracted_scrapers/` (orphaned parallel arch) and the bundled `Deep-Research-...` vendored tree, or move to a clearly-marked `/reference` outside the package and exclude from coverage/imports.
- Remove stray DB artifacts from VCS (`test_audit.db*`, empty `spacescraper_intel.db`, `start_all.bat`); add to `.gitignore`.

**Exit criteria:** `src/` import graph has no unreferenced modules. Tree contains one architecture, not three. ☑ "No orphaned parallel architectures".

### E4. Test coverage to ≥80%
**Action:**
- Unit: ports/adapters (repository, provider, key store), pipeline enrichment/dedup, sanitizer/SSRF (exist), auth validation (new).
- Integration: API → queue → processor happy path on fakeredis; SSRF rejection; rate-limit 429; saga compensation on forced persist failure.
- `pytest --cov=src --cov-report=term-missing`, enforce ≥80% in CI.

**Exit criteria:** Coverage report ≥80% on `src/`. ☑ "Test coverage ≥80%".

### E5. `.env` secret hygiene
**Finding:** `.env` is committed (contains `SLACK_WEBHOOK_URL` placeholder).

**Action:** Ensure `.env` is git-ignored; ship `.env.example` only; rotate any real secret that was ever committed.

**Exit criteria:** No secrets tracked in VCS.

**Phase E result:** Observable, consistent, testable, single coherent architecture. Score impact ~8.5 → **9–9.5**.

---

## 2. Sequencing & dependencies

```
A (security)  ──►  B (ports/DI)  ──►  C (pipeline)  ──►  D (distributed state)  ──►  E (observability/cleanup)
   │                  │                                                                  │
   └─ independent ────┘                                                    E3 (purge) can start any time
```

- **A** ships first and standalone — it is the production blocker.
- **B** is the backbone; **C** and **D** depend on B's ports.
- **E3** (dead-code purge) and **E5** (secrets) are parallelizable from day one.
- **E2** Saga wiring depends on B1 (repository) for real compensation.

## 3. Per-step risk

| Step | Risk | Mitigation |
|------|------|------------|
| A1 key store | Lockout if lookup wrong | Keep dev demo key; add migration to backfill existing keys |
| B1 repository swap | Behavioral drift SQLite↔PG | Shadow-write phase under `features.postgres_db`; compare row parity |
| C1 immutability | `model_copy` perf on large batches | Benchmark; copy only changed fields |
| D2 shared turbo state | Redis latency on hot path | L1 in-proc cache + short TTL; fail-open like existing fan-out check |
| E3 deletions | Removing something still referenced | Grep + full test run before each delete; do in isolated commits |

## 4. Definition of Done (re-audit gate)

Re-run ARCH-AUDIT-V2. Target outcome:
- Phase 2 matrix: no CRITICAL/HIGH rows.
- Phase 5: anti-patterns #1,#2,#3,#4,#5,#6,#7 all resolved.
- Phase 6 score: **≥9/10**, Maturity: **Production**, Refactor Urgency: **Backlog**.

All section-0 checkboxes ticked, each backed by a test, log, or diff.
