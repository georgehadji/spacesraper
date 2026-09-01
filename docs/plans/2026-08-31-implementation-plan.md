# Spacescraper — Implementation Plan: Fixes, Hardening, and the Deep Research Integration

**Companion to:** `docs/plans/2026-08-31-deep-research-integration-architecture.md` (the research; *what* and *why*).
**This document:** the *how* — ordered phases, per-module paradigm and pattern choices with justification, security requirements, and exit criteria.
**Baseline commit:** `d290191` (master).
**Status:** Proposed. Not started.

---

## 0. How to read this

Phases are ordered by **blast radius, highest first** — matching the convention in
`docs/plans/2026-02-26-resilience-hardening.md`. Phase 0 is not optional and not negotiable:
**the API does not currently import.** Everything after it builds on a system that runs.

Each phase carries **Exit criteria** that are testable, not aspirational. A phase is done when its
criteria are provable by a test, a log line, or a grep — the standard already set by
`ARCHITECTURE_REMEDIATION_PLAN.md` §0.

Every phase follows the project's TDD rule: **RED → GREEN → REFACTOR**, one commit per task.

---

## 1. Verified baseline

Everything in this table was **executed or grepped against the tree**, not inferred.

| # | Finding | Evidence | Severity |
|---|---|---|---|
| B1 | **`main.py` cannot be imported.** `RecordsResponse.records: List[...]` at line 381, but line 10 imports only `Any, Dict, Optional`. The annotation is evaluated at class creation → `NameError`. | Reproduced in isolation: `NameError: name 'List' is not defined` | **P0 — API is dead** |
| B2 | `asyncio.create_task` at `main.py:78` with no `import asyncio`. Would `NameError` in `lifespan` even if B1 were fixed. | AST scan: `asyncio imported: False, used: True` | **P0** |
| B3 | `main.py:455-457` branch on `OverlayState.CANARY`. Enum members are `CANDIDATE, SHADOW, ACTIVE, DISABLED, RETIRED`. `AttributeError` on any overlay promotion. | `AttributeError: CANARY` | **P0** |
| B4 | **`http_client` performs no SSRF validation and sets `follow_redirects=True`.** A URL that passes `validate_outbound_url` at the API edge can 302 into `169.254.169.254`. Remediation A2 asked for this guard in the client; it was never added. | `src/infrastructure/http_client.py` — no guard, no redirect hook | **P0 — SSRF bypass** |
| B5 | **API keys live in a per-process dict.** `ApiKeyManager._keys_by_hash` is in-memory; the module-level singleton is per-process. Every issued key dies on restart and is invalid on a second replica. | `auth_middleware.py:84`; its own docstring concedes it | **P1** |
| B6 | **The fan-out cap fails open.** On any Redis error `get_allowed_fanout` returns `requested` (`redis_worker.py:198`), and in mock mode it returns `requested` unconditionally. The repo's own test for it **currently fails**. | `1 failed, 62 passed` — `test_get_allowed_fanout_atomic_with_fakeredis`: expected 50, got 100 | **P1** |
| B7 | Port/adapter contract drift: `RecordRepository.create_record(record)` in `domain/ports.py:54`, but the adapter is `create_record(record, job_id="")` and `job_id` is what `list_records` filters on. The port misstates the real contract. | `ports.py:54` vs `record_repository.py:66` | **P2** |
| B8 | **No `robots.txt` handling anywhere.** Acceptable while targets come from a curated `sources.yaml`; unacceptable once a search engine picks domains. | grep: zero hits | **P1 (blocks Phase 4)** |
| B9 | **No CI.** No `.github/workflows` at all — which is why B1–B3 reached master. | `find` → empty | **P0** |
| B10 | Two orphaned parallel architectures: the vendored `Deep-Research-.../` tree (1.1 MB), and `extracted_scrapers/` (imports a `shared.contracts` package that does not exist). Remediation Phase E counts these against the score. | Import check fails | **P2** |
| B11 | `AIOrchestrator` does not implement the `EnrichmentProvider` ABC, and `main.py` imports the singleton directly. | Remediation B2, still open | **P2** |
| B12 | `AIOrchestrator._compute_embedding_cached` is an `@lru_cache`'d method that unconditionally returns `None` — dead code whose docstring admits it does nothing. Also leaks `self` into the cache key. | `ai/client.py:211-220` | **P2** |

**Test baseline:** 62 passing, 1 failing (B6), 3 modules uncollectable without `playwright` +
`thefuzz` installed. `pytest.ini` sets only `asyncio_mode = auto`.

---

## 2. Guiding principles

These constrain every choice below. They are drawn from `GEMINI.md` and
`ARCHITECTURE_REMEDIATION_PLAN.md` §1 — not invented here.

1. **The dependency rule is law.** `Interfaces → Infrastructure → Application → Domain`. Domain
   imports nothing outward. Application depends on Protocols, never concrete adapters.
2. **Ports before adapters.** Define the Protocol, make the existing concrete class implement it,
   inject it. Only then is a second adapter a drop-in.
3. **"Ponytail" restraint governs pattern choice.** GEMINI.md forbids abstractions without an
   explicit request. §3 therefore justifies **every** pattern as load-bearing and names the patterns
   deliberately *not* used. A pattern that only adds indirection is a defect here, not a virtue.
4. **Fail closed on anything security-relevant.** B4 and B6 are both fail-open defects. New controls
   default to deny.
5. **Feature-flag new capability** via `settings.features`, for shadow rollout — never as
   permanently-dark code.
6. **Identity hash stays pre-AI.** No LLM- or search-derived field may ever feed `identity_hash`
   (GEMINI.md §2), or a prompt change triggers a false-discovery storm.
7. **Additions carry their own tests.** No phase merges below the coverage it started at.

---

## 3. Paradigm and pattern selection, per module

The request was explicitly for the *optimal* paradigm per module. The honest answer is that this
codebase is already predominantly **async procedural code over immutable Pydantic models**, and that
is the right default. Patterns below are introduced **only where they carry weight**.

| Module | Paradigm | Pattern | Why this and not something else |
|---|---|---|---|
| `domain/models.py`, `domain/ports.py` | Pure functional core — immutable Pydantic v2, zero I/O | **Ports (Protocol)**, Value Object | Already the shape. `SearchHit`/`ResearchPlan` are Value Objects: no identity, compared by value, frozen. Ports stay `typing.Protocol` (structural) — matches the file and needs no inheritance from adapters. |
| `providers/search_provider.py` | Async adapter | **Strategy** + **Null Object** (`NoOpSearchProvider`) | Mirrors `enrichment_provider.py` exactly, so no new pattern to learn. Null Object is what makes the feature dark-safe *without* `if enabled:` branches scattered through callers. **Rejected:** a factory/registry — YAGNI at two adapters; a plain dict lookup in composition root suffices. |
| `application/discovery_service.py` | Async procedural, pure decision logic | **Pipeline** (filter chain) | Discovery is a sequence of filters (validate → dedup → budget). A list of small predicate functions composed in order is testable per-filter with no mocking. **Rejected:** Chain of Responsibility — the ceremony buys nothing when the chain is static. |
| `security/url_policy.py` (new) | Pure functions + a small policy object | **Specification** | Allow/deny/robots is a composable boolean rule over a URL. Specification keeps each rule independently testable and combinable. This is the one place the extra structure earns itself: it is the security boundary. |
| `infrastructure/http_client.py` | Async singleton (existing) | **Decorator** over the httpx transport | The SSRF guard must run on *every* request and *every redirect hop*. A custom `httpx.AsyncBaseTransport` wrapper enforces it at the one place all traffic passes, so it cannot be forgotten at a call site. **Rejected:** validating at each call site — that is exactly how B4 happened. |
| `application/synthesis_service.py` | Async procedural over immutable inputs | **Template Method** (thin) | Fixed sequence — gather records → prompt → parse → cite → persist — with the LLM call injected as a port. |
| `application/evaluator.py` (extension) | Functional; pure metric functions | *(no new pattern)* | Extend the existing class. Metric functions stay pure and free of I/O so they are table-testable. |
| `infrastructure/vector_index.py` | Async adapter | **Ports + Adapter**, with a linear-scan default | Default adapter preserves today's behaviour exactly, so the port lands with zero risk before any dependency does. |
| Workers | Async consumer loops (existing) | **Competing Consumers** (existing Streams groups) | Unchanged. The discovery worker joins the existing topology rather than inventing one. |
| Config | Declarative settings | **Composition root** in `main.py` / worker `__main__` | Concretes get chosen in exactly one place per process; everything else receives ports by constructor injection. |

**Patterns explicitly rejected across the board:** Repository-of-repositories, Unit of Work, CQRS,
event sourcing, DI containers, abstract factories, and an agent framework. Each would add
indirection this codebase's stated philosophy forbids, and none is load-bearing for the work below.

---

## PHASE 0 — Restore a working system (P0)

Nothing else can be validated until the API imports and CI exists. Blast radius: total.

### Task 0.1 — Fix the three `main.py` defects (B1, B2, B3)

**Files:** `main.py`, `tests/test_api_imports.py` (new).

- RED: `tests/test_api_imports.py::test_main_module_imports` → `import main`. Fails today with `NameError`.
- GREEN, three surgical edits:
  - line 10: `from typing import Any, Dict, List, Optional`
  - add `import asyncio` to the stdlib import block
  - `PromoteRequest`/`promote_overlay`: remove the `CANARY` branches. The enum has no such member and
    the lifecycle documented in the README is `CANDIDATE → SHADOW → ACTIVE → RETIRED`. **Do not add
    a `CANARY` member** — that is a design change, and `shadow_evaluator.py` already implements
    canary-style staged promotion under the `SHADOW` name.
- Add `test_promote_overlay_rejects_unknown_state` so the enum/route pair cannot drift again.

Note the duplicate `from src.domain.models import ...` on lines 37 and 40 while here; drop the second.

**Exit:** `python -c "import main"` succeeds. Route table enumerable. Both tests green.

### Task 0.2 — CI (B9)

**Files:** `.github/workflows/ci.yml` (new), `requirements-dev.txt` (new).

Minimal and honest — a workflow that lies is worse than none:

```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: python -m pytest -q
      - run: ruff check .
```

Two rules for this workflow:
- **It must run the whole suite**, not the curated list in the README. A curated list is how a red
  test (B6) stays red.
- `playwright` and `thefuzz` are already in `requirements.txt`; browsers are not needed for unit
  tests, so no `playwright install` step. Any test that truly needs a browser gets marked
  `@pytest.mark.browser` and excluded via `-m "not browser"`.

**Exit:** CI runs on this PR and reports. B1–B3 would have been caught by it.

### Task 0.3 — Fix or mark the failing fan-out test (B6)

The test fails because this `fakeredis` version does not implement `EVAL`, so the code takes its
fail-open path. Fix the **test's premise**, not the test's assertion:

- Detect Lua support in a fixture; if absent, `pytest.skip` with an explicit reason — the atomicity
  claim genuinely cannot be tested without it.
- Add a **separate** test asserting the fail-open behaviour is *logged* and observable, so the
  degradation is never silent.
- The fail-open *policy* itself is fixed in Phase 1, not here.

**Never** weaken the assertion to match the buggy result.

**Exit:** `pytest -q` is fully green. No test disabled or deleted.

---

## PHASE 1 — Security foundations (P0/P1)

These must land before Discovery, because Discovery multiplies their blast radius: search-derived
URLs are attacker-influenceable in a way `sources.yaml` targets never were.

### Task 1.1 — Enforce SSRF at the transport (B4) 🔒

The single most important task in this document.

**Files:** `src/infrastructure/http_client.py`, `src/security/ssrf_guard.py`,
`tests/test_security_ssrf_transport.py` (new).

Today `validate_outbound_url` is called at two API call sites and nowhere else, while the shared
client follows redirects blindly. Move enforcement to where **all** traffic passes:

1. Add `GuardedTransport(httpx.AsyncBaseTransport)` wrapping the real transport. On every request —
   **including each redirect hop**, which httpx routes through the transport again — it calls
   `validate_outbound_url` on the target.
2. Harden `ssrf_guard` against **DNS rebinding**, which its own comment flags as unfixed: resolve
   the hostname once, validate the resolved IPs, and connect to the **validated IP** while keeping
   the original `Host` header. Same-resolution guarantee, no TOCTOU window.
3. Keep `follow_redirects=True` — now safe, because every hop is re-validated.
4. Add an explicit `allow_private` escape hatch, default `False`, for tests only.

```python
class GuardedTransport(httpx.AsyncBaseTransport):
    """Enforces the SSRF policy on every request, including redirect hops."""
    def __init__(self, inner: httpx.AsyncBaseTransport, *, allow_private: bool = False):
        self._inner, self._allow_private = inner, allow_private

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self._allow_private:
            validate_outbound_url(str(request.url))   # raises SSRFGuardError
        return await self._inner.handle_async_request(request)
```

**Tests (all must be RED first):** direct fetch of `http://169.254.169.254/` → blocked; a public URL
that **302s to** `169.254.169.254` → blocked at the hop (this is the regression that matters);
`file://` → blocked; DNS name resolving to `127.0.0.1` → blocked; ordinary public URL → allowed.

**Exit:** Discharges Remediation A2 in full. No code path reaches a private address, redirects
included. ☑ *"Every user-supplied URL passes through SSRF."*

### Task 1.2 — Fail the fan-out cap closed (B6) 🔒

**Files:** `src/infrastructure/queues/redis_worker.py`, `tests/test_resilience_fanout_cap.py`.

Fail-open is defensible for a crawl seeded from a curated file. It is not defensible when the seed
set comes from a search engine. Change the policy:

- On Redis error: return a small conservative constant (`FANOUT_DEGRADED_LIMIT = 10`), not
  `requested`, and log a structured WARNING plus a `fanout_degraded_total` metric.
- In mock/dev mode: enforce the cap **in process** rather than returning `requested`, so tests
  exercise real semantics.
- Keep the existing Lua path as the fast, atomic, correct path.

**Exit:** A test with a deliberately broken Redis proves the cap holds at the degraded limit. No path
returns unbounded fan-out.

### Task 1.3 — Persist API keys (B5) 🔒

**Files:** `src/domain/ports.py`, `src/infrastructure/repositories/api_key_repository.py` (new),
`src/auth_middleware.py`, `main.py`.

Remediation A1's validation half is done; its persistence half is not. Add `ApiKeyStore` as a
Protocol beside the other ports (`save`, `get_by_hash`, `revoke`), implement it over Valkey
(`apikey:{key_hash}`, already a hard dependency), and inject it into `ApiKeyManager`.

Security requirements, non-negotiable:
- Store **only** the SHA-256 hash. `POST /auth/register` returns the plain key exactly once.
- Compare with `hmac.compare_digest`, not `==` — a dict lookup on a hash is fine, but any
  fallback comparison path must be constant-time.
- `revoke` is honoured on the read path; a revoked key returns 403, not 401.
- The demo key stays gated on `settings.environment == "development"`.

**Exit:** Registered key survives a restart; unknown key → 401; revoked key → 403; two manager
instances sharing one Valkey agree. ☑ *"Auth validates real, stored keys."*

### Task 1.4 — URL policy: robots + allow/deny (B8) 🔒

**Files:** `src/security/url_policy.py` (new), `tests/test_security_url_policy.py` (new),
`src/config_settings.py`.

**Specification pattern** — each rule is an independently testable predicate:

```python
class UrlPolicy:
    """Composable allow/deny decision for an outbound target."""
    def __init__(self, allowlist: list[str], denylist: list[str], respect_robots: bool = True): ...
    async def is_allowed(self, url: str) -> tuple[bool, str]:   # (decision, reason)
```

- Deny beats allow. An empty allowlist means "any public host" — but Discovery (Phase 4) requires a
  **non-empty** allowlist to be configured, so search cannot target arbitrary hosts by default.
- robots.txt: fetch through the guarded client, honour `Disallow` for our User-Agent, cache per host
  in the existing two-level `AICache` (`provider="robots"`) with a TTL. A fetch failure means
  **deny** for search-discovered URLs and **allow** for explicitly user-submitted ones — the caller
  passes its trust level.
- Honour `Crawl-delay` by feeding it into `DomainRateLimiter.set_budget`.

New settings: `DISCOVERY_ALLOWED_DOMAINS`, `DISCOVERY_DENIED_DOMAINS`, `RESPECT_ROBOTS` (default
`True`).

**Exit:** Policy unit-tested per rule. Denylist beats allowlist. robots `Disallow` respected, and
robots failure denies for discovered URLs.

---

## PHASE 2 — Port hygiene (P2)

Small, mechanical, and a prerequisite for injecting anything new cleanly.

### Task 2.1 — Correct the `RecordRepository` port (B7)

Change the Protocol to `create_record(self, record: ExtractedRecord, job_id: str = "") -> ExtractedRecord`
so the declared contract matches the only real implementation and the `list_records(job_id)` filter
it feeds. Add a conformance test asserting the adapter satisfies the Protocol.

### Task 2.2 — Widen `EnrichmentProvider` and make `AIOrchestrator` implement it (B11, B12)

**Files:** `src/infrastructure/providers/enrichment_provider.py`, `src/infrastructure/ai/client.py`,
`src/application/pipeline.py`.

The ABC declares only `enrich`/`is_available`; the real orchestrator has four more methods and does
not inherit it. Widen the port to the capability set actually used:

```python
async def generate(self, prompt: str, *, timeout: float = 10.0) -> Optional[str]
async def embed(self, text: str) -> Optional[list[float]]
async def generate_overlay(self, html_sample: str) -> Optional[dict]
async def enrich(self, data: dict, prompt_hint: str = "") -> Optional[dict]
async def is_available(self) -> bool
```

Make `AIOrchestrator` implement it; inject into `DataPipeline.__init__` (Remediation B2); delete the
dead `_compute_embedding_cached` (B12) — it returns `None` unconditionally and caches on `self`.

**Exit:** `DataPipeline` constructible with a mock provider — no network, no singleton. Grep shows no
`ai_orchestrator` import inside `src/application/`.

---

## PHASE 3 — Discovery: query → URLs (Increment 7)

The capability gap. Everything before this exists to make it safe.

**Architecture:** a stage **in front of** the pipeline. Scraper, processor, and reporter are
untouched and never learn a job originated from a search result.

### Task 3.1 — Domain models

`src/domain/models.py` — frozen Value Objects, zero infra imports:

```python
class SearchHit(BaseModel):
    model_config = ConfigDict(frozen=True)
    url: str; title: str; snippet: str; rank: int; provider: str

class ResearchPlan(BaseModel):
    plan_id: str; query: str; max_results: int = 10
    allowed_domains: list[str] = Field(default_factory=list)
    serp_artifact_sha: Optional[str] = None      # replay
    state: JobState = JobState.QUEUED
    created_at: datetime = Field(default_factory=datetime.utcnow)
```

`snippet` is **content-hash side only** — never an `identity_hash` input (principle 6).

### Task 3.2 — `SearchProvider` port + adapters

Port in `domain/ports.py`. Adapters in `infrastructure/providers/search_provider.py`, mirroring
`enrichment_provider.py`: ABC, `NoOpSearchProvider` (**the default**), `DuckDuckGoSearchProvider`
(`httpx` — no new dependency), `SerperSearchProvider` (key-gated).

Reuse, do not rebuild: the guarded `http_client`, `AICache` (`provider="search"` — identical queries
are common and SERP calls are billable), `DomainRateLimiter` for the provider host itself, and the
`AIOrchestrator` circuit-breaker shape for provider outages.

### Task 3.3 — `DiscoveryService` (application)

A filter pipeline of small pure-ish steps, each independently testable:

```
hits → dedup_by_canonical_url → UrlPolicy.is_allowed → validate_outbound_url
     → SmartCrawler cache check → fan-out budget → ScrapeJob[]
```

Every rejection is counted by reason and logged structurally — a silently dropped URL is a bug.

### Task 3.4 — Delivery + worker

- `POST /research` — `202 Accepted`, mirroring `POST /jobs`. Sanitize the query with
  `sanitize_for_prompt` + `validate_payload_size` **before** it reaches a third-party API.
  **The search call never happens in the request handler** — it is a third-party network call with
  an unpredictable p95, and `POST /jobs` is async for exactly this reason.
- `GET /research/{plan_id}` — plan state plus child job IDs.
- `MessageType.DISCOVERY_QUERY` — one new enum member; reuse the existing `QueueMessage` envelope
  and its `schema_version`. **No new queue infrastructure.**
- `worker_discovery.py` — consumes `research_stream`, emits ordinary `ScrapeJob`s onto the existing
  `jobs_queue`.
- Archive the raw SERP to the content-addressed `artifact_store`, referenced by
  `ResearchPlan.serp_artifact_sha`, so a research run is replayable against the exact result set.

**Security requirements — all mandatory:**

| Control | Mechanism |
|---|---|
| SSRF | Enforced at transport (1.1) **and** explicitly per hit before enqueue — defence in depth |
| Domain scope | `UrlPolicy` (1.4). Discovery requires a **non-empty allowlist**; empty = refuse to run |
| robots | Honoured; fetch failure denies for discovered URLs |
| Prompt injection | Query sanitized at the edge; SERP snippets are **data**, never instructions, and must never be concatenated into a prompt that also carries system directives |
| Blast radius | `get_allowed_fanout` with a discovery cap **well below** the crawl cap of 200 — start at 25 |
| Cost | Existing `ai_cost_per_hour` SLO; add `discovery_queries_per_hour` |
| Auth | `POST /research` requires an API key like every other mutating route |

**Feature flag:** `features["discovery"]`, default `False`. `NoOpSearchProvider` is the default
adapter, so the feature is inert until both are set.

**Exit:** A query yields N validated jobs that flow through the unmodified pipeline. Integration
tests prove: a query returning a private-IP host enqueues nothing; a query with an empty allowlist
is refused; the fan-out cap holds; the SERP is replayable from the artifact store.

---

## PHASE 4 — LLM providers, including local models (Increment 8)

Depends on Task 2.2.

### Task 4.1 — `LocalLLMProvider` over HTTP

**Do not import `torch`/`transformers` into any worker.** A 4-bit Gemma-2B resident in the scraper
process invalidates `BrowserContextPool` memory assumptions and the `pool_size` arithmetic, and
makes workers non-scalable horizontally.

Run the model behind an OpenAI-compatible endpoint (Ollama, `llama.cpp` server, vLLM). The adapter
is then a thin `http_client` call implementing the widened port — the circuit breaker, retry with
backoff, and `AICache` all keep working unchanged, and **no new dependency is added**.

Settings: `AI_PROVIDER` (`gemini` | `local` | `noop`), `AI_LOCAL_BASE_URL`, `AI_LOCAL_MODEL`.
Selection happens in the composition root only.

**Security:** the local endpoint is typically on a private address, so it must use the
`allow_private=True` client explicitly and **only** for that configured host — never a
general relaxation of the guard.

**Exit:** Provider swappable by config with no call-site change. Contract tests run against every
adapter, including `NoOp`.

---

## PHASE 5 — LLM output quality inside the existing loop (Increment 9)

Spacescraper already has the evaluation loop the vendored project wanted
(`StrategyObservation → StrategyEvaluator → DomainProfile`, `SLOMonitor` auto-rollback,
bounded `ExplorationPolicy`). It scores extraction but never the LLM's own output. Close that gap
**inside the existing tables** — `StrategyObservation.strategy` is already a free string.

### Task 5.1 — Schema migration

Add two nullable columns to `strategy_observations` and their model fields:
`groundedness REAL`, `citation_coverage REAL`. Nullable so existing rows stay valid; SQLite
`ALTER TABLE ADD COLUMN` is safe and non-locking. Add the same to the Postgres path.

### Task 5.2 — Pure metric functions

In `src/application/llm_metrics.py` — no I/O, table-testable:

- `groundedness(claims, sources) -> float` — fraction of LLM claims traceable to a source record or
  SERP snippet, by token overlap with an optional embedding-similarity refinement.
- `citation_coverage(answer) -> float` — fraction of sentences carrying a `record_id` citation.

Both computable **without** any evaluation framework, because the content-addressed artifact store
already retains the sources.

### Task 5.3 — Wire it

Record `strategy="llm_extract"` / `"llm_synthesis"` observations from the LLM paths.
`StrategyEvaluator` then scores those strategies with its existing `score`/`recommendation`
machinery. Add an `llm_groundedness` SLO (warn 0.7 / crit 0.5, matching the existing threshold
style) so a regressing prompt or model **auto-rolls back**. `ExplorationPolicy` bounds how often the
LLM path runs at all (5% default).

**Rejected:** `trulens`, `wandb`, `deepeval`, `evaluate`. The loop exists; it needed a metric, not
a framework.

**Exit:** An LLM strategy appears in `EvaluationResult` rows with a `recommendation`. A forced
groundedness regression trips the SLO and rolls back. Zero new dependencies.

---

## PHASE 6 — Synthesis (Increment 10)

`SynthesisService` reads the `ExtractedRecord`s for a `root_job_id`, asks the LLM for an answer with
**per-claim `record_id` citations**, writes it to `artifact_store`, and emits the existing
`DiscoveryEvent` — which `worker_reporter` already fans out to Slack, webhooks, and file exports.

Mandatory citations are what make Phase 5's groundedness metric computable, so the two phases
reinforce each other. Uncited claims are **dropped, not published**.

**Security:** record data is untrusted third-party content. It is passed as data with an explicit
instruction that it is data; the prompt never interpolates it into a directive position. Output is
schema-validated before persistence.

**Exit:** A research plan produces one cited answer artifact; every claim resolves to a stored
`record_id`; uncited claims are dropped and counted.

---

## PHASE 7 — Vector index (Increment 11, conditional)

**Do not start this without a measurement showing it is needed.**

Today dedup is a linear scan over JSON-decoded vectors, and `_embedding_cache` is per-instance.

1. Do Remediation **C3** first — Redis-backed embedding cache with a small L1 LRU in front. Already
   planned, no new dependency, and it removes the per-process duplication that dominates cost today.
2. Add a `VectorIndex` port (`upsert(id, vector)` / `query(vector, k)`) whose **default adapter is
   the current linear scan** — behaviour identical, risk zero.
3. A FAISS or Chroma adapter lands **only** once a domain's record count crosses a measured
   threshold, behind `features["vector_index"]`.

**Exit:** Port in place with unchanged behaviour; the dependency is added when a measurement demands
it, never on spec.

---

## PHASE 8 — Delete the orphans (B10)

Remediation Phase E counts orphaned parallel architectures as a score-capping anti-pattern.

- `Deep-Research-With-Web-Scraping-by-LLM-And-AI-Agent-main/` — 1.1 MB of notebooks. Everything of
  value is captured in the research doc. **Delete.**
- `extracted_scrapers/` — imports a `shared.contracts` package that does not exist; it cannot run.
  Its *ideas* (cost-aware planning, governance gating) are already realised by `ExplorationPolicy`
  and the overlay lifecycle. **Delete, or wire one module and delete the rest.**

This is the one phase requiring an explicit human decision — deleting a vendored tree is not
something to infer. It is listed last so nothing else depends on it.

---

## 4. Cross-cutting requirements

### Testing

- Every phase is RED → GREEN → REFACTOR, one commit per task.
- **Contract tests per port**, run against every adapter including `NoOp` — this is what makes the
  ports real rather than decorative.
- Security tests are **negative** tests: assert the bad thing is *refused*. Redirect-to-metadata,
  denylisted domain, robots `Disallow`, empty allowlist, revoked key, degraded fan-out.
- Never weaken an assertion to match buggy behaviour (see Task 0.3). Never skip, disable, or
  quarantine a test to get green.
- Target ≥ 80% on `src/`, per the remediation rubric.

### Observability

Every new decision point emits a structured log with the `correlation_id` and increments a metric:
`discovery_hits_total`, `discovery_rejected_total{reason}`, `robots_denied_total`,
`fanout_degraded_total`, `ai_degraded_total`, `llm_groundedness`. A silent rejection is a defect —
that principle is what B6 violated.

### Configuration

New settings all default to **off/deny**:

```
DISCOVERY_ENABLED=false          SEARCH_PROVIDER=noop
SEARCH_API_KEY=                  DISCOVERY_ALLOWED_DOMAINS=
DISCOVERY_DENIED_DOMAINS=        DISCOVERY_MAX_FANOUT=25
RESPECT_ROBOTS=true              AI_PROVIDER=gemini
AI_LOCAL_BASE_URL=               AI_LOCAL_MODEL=
```

Mirror into `.env.example` and the README's config table.

### Rollout and rollback

Each capability ships dark: flag `False` + `NoOp` adapter. Enable in shadow first — Discovery
enqueues to a shadow stream and records observations without producing records — then promote once
its observations clear the evaluator, reusing the overlay lifecycle machinery rather than inventing
a second one. Rollback is a flag flip; no schema change in Phases 3–6 is destructive.

---

## 5. Sequencing

```
Phase 0 (P0)  ─┬─► Phase 1 (P0/P1) ──► Phase 2 ─┬─► Phase 3  (Discovery)
  fixes + CI   │     SSRF, fan-out,   port       │      needs 1.1 + 1.4
               │     auth, robots     hygiene    ├─► Phase 4  (providers, needs 2.2)
               │                                 │        └─► Phase 5 (LLM metrics)
               │                                 │                └─► Phase 6 (synthesis)
               └─────────────────────────────────┴─► Phase 7 (vector, conditional)
                                                     Phase 8 (deletions, human decision)
```

**Hard gates:**
- Phase 0 blocks everything. The API does not import.
- **Phase 1 blocks Phase 3.** Shipping Discovery before the transport-level SSRF guard and the URL
  policy would take an attacker-influenceable URL source and point it at a client that follows
  redirects into private space with an unbounded, fail-open fan-out. That combination is the single
  most dangerous state this plan can pass through, and this ordering exists to prevent it.
- Phase 2.2 blocks Phase 4. Phase 5 blocks Phase 6's quality claims.

**Dependency budget:** Phases 0–6 add **no runtime dependencies** (`requirements-dev.txt` adds
`ruff` for CI only). Phase 7 adds at most one, conditionally.

---

## 6. Threat model summary

| Threat | Vector | Control | Phase |
|---|---|---|---|
| SSRF to cloud metadata | Submitted URL, discovered URL, or **redirect hop** | Transport-level guard on every hop + IP pinning | 1.1 |
| DNS rebinding | Re-resolution between check and connect | Validate resolved IP, connect to that IP, preserve `Host` | 1.1 |
| Crawler weaponized as DDoS | Query fanning out to unbounded jobs | Fail-**closed** fan-out, discovery cap 25, per-domain rate limiter | 1.2, 3 |
| Unauthorized API use | Keys lost on restart / not shared across replicas | Persistent hashed key store, revocation honoured | 1.3 |
| Scraping disallowed content | Search picks arbitrary domains | `UrlPolicy`: allowlist required, denylist wins, robots honoured | 1.4 |
| Prompt injection via page or SERP content | Hostile page text reaching an LLM prompt | Sanitize at edge; scraped content passed as data, never in directive position; output schema-validated | 3, 6 |
| Data exfiltration via webhook | Attacker-supplied `webhook_url` | Existing `require_https` + SSRF guard, now enforced at transport too | 1.1 |
| Secret leakage in logs | API keys, query strings | Existing `sanitize_for_log`; extend to search queries and provider keys | 1.4 |
| Silent degradation | Fail-open controls, dropped URLs | Every rejection logged + counted; SLO alerts | cross-cutting |

---

## 7. What this plan deliberately does not do

- **No agent framework.** Free-running CrewAI/LangChain agents would delete the human-gated overlay
  lifecycle (`/overlays/{id}/promote` requires `human_approved=True`) and the auto-rollback net that
  Increments 5–6 exist to provide. This repo's own `extracted_scrapers/governance_agent.py` states
  the rule: *"Never activates selectors automatically."*
- **No second fetch path.** crawl4ai or Selenium would bypass `DomainRateLimiter`, `stealth_brain`,
  personas, the proxy manager, the SSRF guard, and the artifact store.
- **No `torch` in a worker.** Phase 4 covers local models without it.
- **No evaluation framework.** Phase 5 adds the metric the loop was missing.
- **No `CANARY` enum member.** `shadow_evaluator.py` already implements staged promotion under
  `SHADOW`; adding a member to satisfy dead code is backwards.
- **No speculative abstraction.** §3 names every rejected pattern and why.
