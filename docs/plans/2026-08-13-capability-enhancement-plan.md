# Spacescraper Capability Enhancement Plan

**Date:** 2026-08-13
**Branch at time of writing:** `fix/e2e-correctness-and-headless-cli`
**Source:** GitHub scraping-ecosystem research pass (2026-08-13) + full codebase capability inventory (2026-08-13)
**Relationship to other plans:** Builds on top of `2026-08-10-architecture-remediation-to-8.5.md`. That plan repairs what exists; this plan adds what is missing. Phase 0 here overlaps deliberately with remediation W2/W3 — those items are prerequisites and are referenced, not duplicated.

---

## Table of Contents

- [1. Competitive Landscape Summary](#1-competitive-landscape-summary)
- [2. Gap Analysis](#2-gap-analysis)
- [3. Design Principles](#3-design-principles)
- [4. Phases](#4-phases)
  - [P0 — Reconnect the Live Path (prerequisite)](#p0--reconnect-the-live-path-prerequisite)
  - [P1 — Adaptive Fetch Tiering](#p1--adaptive-fetch-tiering)
  - [P2 — Actual Crawling: Frontier, Robots, Sitemaps, Pagination](#p2--actual-crawling-frontier-robots-sitemaps-pagination)
  - [P3 — Session Pool, Proxy Wiring, Stealth Feedback Loop](#p3--session-pool-proxy-wiring-stealth-feedback-loop)
  - [P4 — LLM Extraction Economics](#p4--llm-extraction-economics)
  - [P5 — API Surface: Verb Taxonomy and Change Tracking](#p5--api-surface-verb-taxonomy-and-change-tracking)
  - [P6 — Operations: Scheduling, Autoscaling, Real Telemetry](#p6--operations-scheduling-autoscaling-real-telemetry)
- [5. Sequencing and Dependencies](#5-sequencing-and-dependencies)
- [6. Explicit Non-Goals](#6-explicit-non-goals)
- [7. Risk Register](#7-risk-register)

---

## 1. Competitive Landscape Summary

Research pass covered the leading open-source scraping projects (verified 2026-08-13): Scrapy (63.8k★, now asyncio-native), Crawlee Node/Python (25.4k/9.4k★), Scrapling (73.8k★), crawl4ai (78k★), Firecrawl (166.8k★), ScrapeGraphAI (29.5k★), the stealth component stack (patchright, camoufox, nodriver, curl_cffi), browser-agent tools (browser-use 109k★, Skyvern, Stagehand), and niche tools (changedetection.io 33k★, MediaCrawler 62k★, botasaurus, Lightpanda).

The patterns that recur among the leaders, ranked by leverage for this codebase:

| # | Pattern | Where it appears | Why it matters here |
|---|---|---|---|
| 1 | **Adaptive HTTP-vs-browser decision** — try cheap HTTP first (with TLS impersonation), escalate to a browser only on JS-need or block detection, cache the per-domain decision | Crawlee `AdaptivePlaywrightCrawler`, crawl4ai escalation, Scrapling fetcher tiers | Spacescraper launches Chromium for everything (`cli.py --browser` aside). Largest cost/throughput win available |
| 2 | **LLM-generate-once, reuse-forever extraction** — LLM emits a CSS/XPath schema once; subsequent pages run deterministically; LLM re-invoked only when the schema stops validating | crawl4ai schema generation, Stagehand action caching | The overlay system (`/autograph`, `OverlayRepository`, shadow evaluator) *is* this pattern — built but not consulted on the live path |
| 3 | **Selector self-healing** — store element context signatures; relocate elements after site redesigns before falling back to the LLM | Scrapling (signature feature), Stagehand | `AIOrchestrator.heal_selector` exists (`src/infrastructure/ai/client.py`) with no caller |
| 4 | **Session pool with health scoring + coherent fingerprint personas** — retire blocked sessions; sample whole realistic fingerprints; derive locale/timezone/geo from proxy IP | Crawlee SessionPool, camoufox/BrowserForge | `persona.py` generates fingerprints but nothing scores or retires them; `stealth_brain.py` learns winners that are never read back |
| 5 | **Resource-aware autoscaled concurrency** | Crawlee AutoscaledPool, crawl4ai MemoryAdaptiveDispatcher | Workers run fixed concurrency; queue already has memory backpressure to build on |
| 6 | **Fit-markdown content pruning before LLM calls** | crawl4ai (heuristic pruning + BM25) | Complements existing `html_compactor.py` and the recent token-cost commits |
| 7 | **scrape/crawl/map/extract verb taxonomy + async job contract** | Firecrawl | Current API is jobs-only; no map (URL discovery) or synchronous scrape verb |
| 8 | **Change tracking as a first-class scrape output** | Firecrawl, changedetection.io | `identity_hash`/`content_hash`/`ChangeType` fully modeled; dead on the live path (P0) |
| 9 | **Spider contracts / fixture regression tests for extractors** | Scrapy contracts | Extraction strategies have unit tests but no stored-fixture contract harness |
| 10 | **In-page JS signing execution** for API-first extraction | MediaCrawler | Relevant to the Maps strategies, which already intercept XHR |

Full research report and capability matrix retained in the session transcript; the matrix rows above are the subset that survived gap analysis against the inventory below.

---

## 2. Gap Analysis

### What Spacescraper already does at or above the ecosystem bar

- **Queue semantics** (`src/infrastructure/queues/stream_queue.py`): consumer groups, XCLAIM recovery, DLQ, idempotent consumption, memory backpressure, Lua fan-out budgets. Richer than anything in Scrapy or crawl4ai; comparable to Firecrawl's BullMQ setup.
- **Job lifecycle** (`src/domain/models.py`): explicit state machine, optimistic concurrency, idempotency keys, heartbeats, soft delete. Above the bar — most frameworks have nothing comparable.
- **Security**: fail-closed SSRF guard with DNS-rebinding TOCTOU closure (`src/security/validating_transport.py`), hashed API keys, tiered quotas, log redaction. No open-source scraper in the survey ships this.
- **LLM cost controls**: HTML compaction as cache key, two-level AI cache, circuit breaker + bulkhead. On par with crawl4ai's best practices.
- **Extraction dispatch**: 6-stage priority chain (override → Maps place → Maps search → overlay → JSON-LD → semantic HTML) is a sound Chain of Responsibility.

### Confirmed capability gaps (verified absent, not merely unread)

| Gap | Ecosystem norm | Evidence of absence |
|---|---|---|
| Link discovery / recursive crawling | Universal (all Tier-1 frameworks) | `follow_urls` hardcoded `[]`; `extraction_pipeline.py` states no live strategy constructs `FollowLink`. Consumer side fully built (`worker_processor.py` fan-out caps) |
| robots.txt respect | Scrapy, Crawlee, Firecrawl default-on | Zero source hits for "robots" |
| Sitemap / feed discovery | SitemapSpider (Scrapy/Scrapling), /map (Firecrawl), URL seeding (crawl4ai) | Zero source hits |
| Auto-pagination / infinite scroll | Universal | Only API-response cursor pagination exists |
| HTTP-first fetch with TLS impersonation | Crawlee, Scrapling, curl_cffi stack | Chromium-only on the worker path; plain httpx has no fingerprint impersonation |
| Adaptive rendering decision | Crawlee (best-in-class) | No mechanism; `StrategySelector` writes `preferred_strategy` that nothing reads |
| Working proxy rotation | Universal | `ProxySessionManager` has zero instantiations; `ScrapeJob.use_proxy` has zero readers |
| Session persistence / health scoring | Crawlee SessionPool | Session cookie methods are `return []` / `pass` stubs |
| Scheduling / recurring crawls | changedetection.io, Firecrawl, botasaurus | `apscheduler` declared in requirements, never imported |
| Resource-aware autoscaling | Crawlee, crawl4ai | Fixed worker concurrency |
| Markdown-for-LLM output format | crawl4ai, Firecrawl, Scrapling | Compactor produces compacted HTML only |
| Multi-browser / cheap-renderer tier | Crawlee, Lightpanda | `pool.py` launches Chromium only |
| Selector self-healing | Scrapling | `heal_selector` exists, uncalled |
| Dashboard / operator UI | crawl4ai, botasaurus, changedetection.io | No frontend; docstrings reference a dashboard that does not exist |

### Built-but-dead subsystems (repair, don't rebuild)

Change detection (post-processor filters on deprecated `Opportunity` type while live path emits `ExtractedRecord`), outbox relay (never started in lifespan), conditional-request HTTP cache (`smart_crawler` singleton constructed with `None` client), stealth-brain feedback (`get_best_attributes` never called), strategy-selector output (never read), shadow evaluator, exploration policy, auto-rollback, image downloader, `bootstrap.py` composition root. These are P0/remediation-plan territory: **every new capability in P1–P6 must land on a reconnected live path, or it joins the graveyard.**

---

## 3. Design Principles

These extend the per-module paradigm table in `2026-08-10-architecture-remediation-to-8.5.md` §1 and govern all new code in this plan.

1. **New capability = new Port + adapter, never a new root-level module.** Every phase below names its Protocol in `src/domain/ports.py` or a phase-specific ports module. The import-linter contract stays green.
2. **Chain of Responsibility for escalation ladders.** The fetch tier (P1) and the extraction-repair ladder (P4) are ordered chains with explicit fallthrough, mirroring the existing extraction dispatch — one idiom for one shape of problem.
3. **Policy objects for decisions, separated from mechanisms.** Rendering decisions, politeness rules, session retirement, and scheduling are pure, synchronous, unit-testable policy classes in `src/application/`; I/O adapters execute their verdicts.
4. **Learn via the observation loop that already exists.** `ObservationRepository` + `Evaluator` + `StrategySelector` form a working feedback substrate. New adaptive behavior (fetch-tier choice, session scoring, selector health) records outcomes there instead of inventing parallel stores.
5. **Functional core, imperative shell.** Frontier scoring, robots evaluation, markdown pruning, and diff computation are pure functions over immutable inputs (`frozen=True` Pydantic), pushed to `src/domain/` or pure application services. Effects stay in adapters.
6. **One capability, one implementation.** The inventory found three LLM enrichment implementations and two domain models. Each phase explicitly deletes or refuses to create the second implementation of anything.
7. **Politeness fails closed; budget caps fail open.** robots.txt denial and SSRF are hard stops. Fan-out budgets and rate limits degrade gracefully (existing behavior, kept).

---

## 4. Phases

### P0 — Reconnect the Live Path (prerequisite)

Not new work — this is remediation-plan W2/W3 restated as entry criteria. This plan's phases must not begin until:

- [x] Post-processor operates on `ExtractedRecord` (kills the `isinstance(entity, Opportunity)` filter), so change detection, discovery events, reporter, and `record_count` function. (Remediation C2/W2.3)
- [x] Overlay promotion affects live extraction — the pipeline consults `OverlayRepository.get_active_overlay(domain)` on the live path. (Remediation C1/W3.1)
- [x] `OutboxRelay.run_forever` runs as a lifespan background task alongside `StrategySelector`'s loop.
- [x] Worker consumer names derive from hostname + PID instead of hardcoded `"scraper-1"`/`"processor-1"`, so replica scale-out gets real XCLAIM recovery.
- [x] `bootstrap.py` becomes the single composition root used by `main.py` and workers (Remediation W4), because P1–P6 each add constructor-injected dependencies and cannot sanely wire them through module-level globals.
- [x] A reaper task calls `find_stale_jobs` / `purge_expired_jobs` (both currently caller-less).

**Result (2026-08-22):** Items 1-3 were already done pre-session (post_processor.py:9,34 uses `ExtractedRecord`; extraction_pipeline.py:212-215 consults `get_active_overlay`; main.py lifespan already started both loops). Items 4-6 closed this session:
- Consumer names: [worker_scraper.py](../../worker_scraper.py) and [worker_processor.py](../../worker_processor.py) now build `f"{role}-{socket.gethostname()}-{os.getpid()}"` and pass it to `stream_queue.consume(...)`.
- Composition root: [src/bootstrap.py](../../src/bootstrap.py) now holds the real `AppContainer` (moved from main.py's inline dataclass — main.py imports it instead). Both workers' `__main__` entrypoints build their service from `bootstrap.container`'s repos/queue rather than each constructing its own. Constructor-level DI defaults in the worker classes were left untouched (existing tests inject explicit fakes; no behavior change there).
- Reaper: new [src/application/reaper.py](../../src/application/reaper.py) (`JobReaper`), wired as a third lifespan task in main.py alongside `strategy_selector`/`outbox_relay`. Tested in [tests/test_reaper.py](../../tests/test_reaper.py).

429/429 tests pass; mypy clean on `src/domain`. Gate is green — P1-P6 may begin.

### P1 — Adaptive Fetch Tiering

**Goal:** stop paying Chromium prices for HTTP-priced pages. Borrow Crawlee's adaptive crawler and Scrapling's fetcher tiers.

**Design — Chain of Responsibility behind one Port:**

```
FetcherPort (Protocol, src/domain/ports.py)
    fetch(request: FetchRequest) -> FetchResult   # FetchResult carries html, status,
                                                  # tier_used, block_signals, timing

Tier 1  ImpersonatingHttpFetcher   curl_cffi, per-request impersonate="chrome"
Tier 2  StealthBrowserFetcher      existing BrowserContextPool + persona (Playwright)
(Tier 3 reserved: cheap DOM-only renderer, e.g. Lightpanda via CDP — non-goal for now)
```

- `AdaptiveFetchService` (application layer) owns the chain. Per request: consult `RenderingPolicy`; run chosen tier; on block signal or JS-required signal, escalate and **record the outcome as a `StrategyObservation`**.
- `RenderingPolicy` is a pure policy object backed by `DomainProfile` (which already has a `preferred_strategy` slot — this finally gives it a reader). Decision inputs: past tier outcomes for the domain, block rate, content-delta between HTTP and browser fetches (sampled). Crawlee's approach: occasionally double-fetch to re-validate the cached decision.
- Block detection generalizes the existing four-title-string check in `engine.py` into a `BlockSignalDetector` (status codes 403/429, challenge markers, content-length collapse, Cloudflare/Turnstile fingerprints) shared by both tiers.
- `smart_crawler`'s conditional-request cache (ETag/Last-Modified) gets its Valkey client injected via the composition root and becomes Tier 1's cache layer — repairing the `None`-client no-op found in the inventory.

**New dependency:** `curl_cffi`. **Patterns:** Chain of Responsibility, Policy object, Strategy (tiers), Cache-Aside.

**Deliverables:**
- [x] `FetcherPort` + two adapters; `AdaptiveFetchService`; `RenderingPolicy` with persisted per-domain decision
- [x] `worker_scraper` fetches through `AdaptiveFetchService` (rate limiter and SSRF transport preserved on both tiers — curl_cffi requests must route through the same SSRF validation the httpx transport enforces)
- [x] Metrics: `fetch_tier_http`, `fetch_tier_browser`, `tier_escalations`, per-tier latency in the existing observability counters
- [x] Contract tests: blocked-HTTP fixture escalates; plain-HTML fixture never launches a browser

**Result (2026-08-22):** `FetcherPort` in [ports.py](../../src/domain/ports.py); `FetchRequest`/`FetchResult` in [fetch.py](../../src/domain/fetch.py). Tier 1 = [ImpersonatingHttpFetcher](../../src/infrastructure/fetch/http_fetcher.py) (curl_cffi, `impersonate="chrome"`); Tier 2 = [StealthBrowserFetcher](../../src/infrastructure/fetch/browser_fetcher.py) (wraps `ScraperEngine`). `RenderingPolicy` ([rendering_policy.py](../../src/application/rendering_policy.py)) is a pure function over `DomainProfile.preferred_strategy`/`block_rate`; [AdaptiveFetchService](../../src/application/adaptive_fetch.py) owns the attempt-and-escalate decision, persisting a demotion to `preferred_strategy="browser"` via `ObservationRepository.update_profile` on any block/failure.

Two scope calls, documented rather than silently shipped:
- **worker_scraper.py's own Tier-2 execution stays on the existing `ScraperEngine` path**, not routed through `StealthBrowserFetcher`. That wrapper only returns a generic `FetchResult` (html/status), but the worker's browser path needs the richer `RawScrapePayload` (intercepted JSON endpoints for turbo mode, `engine.persona` for the stealth-brain feedback loop) — forcing it through the narrower port would lose both. `StealthBrowserFetcher` exists as a real, usable Tier-2 adapter for other future callers (P2 SitemapSeeder, P5 `/scrape`) that only need HTML back.
- **SSRF on Tier 1 (R2) is a pre-flight `validate_outbound_url()` check, not a transport-level guard.** curl_cffi has no hook to bind the resolved IP the way `validating_transport.py`'s httpx transport does for the DNS-rebinding TOCTOU window — a small residual gap versus Tier 2's tighter guard, noted inline in `http_fetcher.py`.

Wired into `worker_scraper.py._process_job`: after a turbo-endpoint miss (or no turbo endpoints known), Tier 1 is attempted before the browser; a clean hit finalizes the job via a new shared `_finalize_success` helper (extracted from the pre-existing browser success path, DRY) and returns without ever constructing `ScraperEngine`. `curl_cffi>=0.16.1` added to requirements.txt. Tests: [test_adaptive_fetch.py](../../tests/test_adaptive_fetch.py) (6 tests, the plan's exact blocked/clean fixtures). 444/444 tests pass; mypy clean on `src/domain`.

### P2 — Actual Crawling: Frontier, Robots, Sitemaps, Pagination

**Goal:** the inventory's verdict was "it scrapes URLs you hand it; it does not discover them." Fix that. The consumer half (fan-out caps, depth fields, `FollowLink` handling in `worker_processor.py`) already exists — this phase builds the producer half.

**Design:**

- **`LinkDiscoveryService`** (application): pure function core `extract_links(html, base_url, rules) -> list[FollowLink]` with glob/regex include-exclude rules (Crawlee `enqueueLinks` semantics). Wired into the extraction pipeline so strategies stop hardcoding `follow_urls=[]`. Frontier dedup rides the queue's existing idempotent-consumption keys, plus a crawl-scoped seen-set (Valkey SET per root job) for request fingerprinting Scrapy-style: canonicalized URL hash.
- **`PolitenessGate`** (domain policy + infra adapter): `RobotsPort` with an adapter that fetches/caches `robots.txt` per domain (TTL in Valkey), evaluated **fail-closed** before any fetch, honoring `crawl-delay` by feeding it into the existing per-domain rate limiter. Config flag to relax for explicitly-owned targets, default on — matching Scrapy/Firecrawl defaults.
- **`SitemapSeeder`**: given a root URL, discover `sitemap.xml` (robots.txt `Sitemap:` lines + well-known paths), parse recursively (index files), emit seed jobs. Exposed as the `/map` verb in P5 and as `cli.py map`. crawl4ai-style prefetch: HEAD-check candidate URLs cheaply through Tier 1.
- **`PaginationStrategy`** joins the extraction dispatch chain: detects `rel=next`, common next-page selectors, and URL patterns (`page=N`), emitting `FollowLink(kind="pagination", depth=same)`. Infinite scroll stays browser-tier: a bounded scroll-and-settle loop in `engine.py` (max scrolls, content-growth cutoff).
- Crawl orchestration model: a `CrawlSpec` (max_depth, max_pages, include/exclude globs, strategy hint) attached to `ScrapeJob` — fields already exist (`max_depth`, `depth`); add the glob rules. BFS by default; Best-First with a pluggable scorer (crawl4ai pattern) as a follow-up inside the same interface.

**Patterns:** Specification (include/exclude rules), Policy object (politeness), pure-function link extraction, Producer side of existing fan-out.

**Deliverables:**
- [ ] `LinkDiscoveryService` + pipeline wiring; live-path job with `max_depth=2` demonstrably enqueues children within fan-out budget
- [ ] `RobotsPort` + fail-closed gate + crawl-delay integration; politeness bypass flag documented
- [ ] `SitemapSeeder` + `cli.py map <url>` returning discovered URLs as JSON
- [ ] `PaginationStrategy` in the dispatch chain + bounded infinite-scroll driver
- [ ] Resilience test: crawl of depth 3 respects fan-out cap and dedups revisits

### P3 — Session Pool, Proxy Wiring, Stealth Feedback Loop

**Goal:** make the stealth machinery a closed loop. Borrow Crawlee's SessionPool health scoring and camoufox's coherent-persona principle.

**Design:**

- **`Session`** (domain value object): persona_id + proxy + cookie jar + health score + age. **`SessionPool`** (infrastructure): leases sessions to fetches, scores outcomes (success +1, block −3, retire below threshold or after N uses — Crawlee's error-score model), persists cookie jars (repairing the `return []`/`pass` stubs in `proxies/manager.py`, which this replaces — delete that module rather than keeping a second implementation).
- **Proxy wiring:** `ProxyProviderPort` (static list from config now; provider APIs later). Both fetch tiers accept the leased session's proxy: curl_cffi `proxies=`, Playwright context `proxy=`. `ScrapeJob.use_proxy` finally gets a reader.
- **Coherent personas:** persona derives locale/timezone/Accept-Language from the session's proxy geo (camoufox's principle; a static country→locale table now, geo-IP lookup later). Persona and proxy are bound for the session's lifetime — never rotate one without the other.
- **Close the stealth-brain loop:** `PersonaFactory` seeds new personas from `stealth_brain.get_best_attributes()` (finally giving the learned winners a reader) with an exploration fraction reusing `exploration_policy.py`'s bounded 5% explore — fixing its Thompson-sampling `_exploit` to actually sample Beta rather than take the mean, and implementing its no-op `record_outcome`. This rescues two dead modules with their intended semantics.
- CAPTCHA solving remains **detection-only** (existing behavior). Solver integration is explicitly out of scope — see Non-Goals.

**Patterns:** Object Pool with health scoring, Value Object, Factory, bandit-style explore/exploit.

**Deliverables:**
- [ ] `SessionPool` + cookie persistence + retirement; `proxies/manager.py` deleted
- [ ] Proxy plumbed through both fetch tiers from config
- [ ] Persona-proxy coherence + stealth-brain feedback with sampled exploration
- [ ] Metrics: session retirements, block rate by persona cohort
- [ ] Tests: blocked session retires; persona/proxy binding survives lease/release

### P4 — LLM Extraction Economics

**Goal:** LLM as schema author and repair crew, never as per-page extractor. The overlay system already embodies generate-once/reuse-forever — finish it and add the self-healing rung.

**Design — extraction-repair ladder (Chain of Responsibility, again):**

```
1. Active overlay extracts cleanly            → done (deterministic, zero LLM cost)
2. Overlay yields but fails schema validation → SelectorHealer: heal_selector()
   (existing, uncalled) repairs the failing selector using stored element context
   signatures (Scrapling pattern); healed overlay saved as CANDIDATE
3. Healing fails                              → autograph regeneration (existing
   /autograph internals) from current HTML; new CANDIDATE overlay
4. CANDIDATE overlays flow through the shadow evaluator (existing, currently
   caller-less — wire ShadowEvaluator.evaluate into worker_processor on jobs for
   domains with candidates) and promote via the existing human-approved gate +
   AutoRollback regression guard (existing, currently dead)
```

- **Element context signatures:** when an overlay is created, store per-field anchor context (tag, classes, text head, parent chain hash) alongside `field_mappings` in the overlay schema. Healing matches signatures against the new DOM before asking the LLM — Scrapling's relocation idea, cheap first rung.
- **Fit-markdown:** extend `html_compactor.py` with a `to_markdown(html, query=None)` mode — structural pruning plus BM25 relevance filtering against the job's schema field names (crawl4ai's fit-markdown). Used (a) as the LLM input for autograph, cutting tokens further, and (b) as a user-facing output format: `ExtractedRecord` gains an optional `markdown` artifact stored via the existing `ArtifactStore`.
- **Consolidation (principle 6):** delete `src/application/llm_enrichment.py` (mock-riddled OpenAI path, imported by nothing) and fold `enrichment_provider.py`'s ABC into the single Gemini-backed `AIOrchestrator` behind an `LLMProviderPort` — one enrichment implementation, provider-swappable per the remediation plan's W-series intent.
- **Spider contracts:** fixture harness `tests/contracts/` — stored HTML snapshots per domain strategy with expected-record assertions (Scrapy contracts idea, filesystem edition). Every overlay promotion writes its evaluation sample HTML into the fixture set via the artifact store, so regressions in Maps/overlay strategies are caught in CI, not production.

**Patterns:** Chain of Responsibility (repair ladder), Memento (element signatures), single Port for LLM provider, golden-file contract testing.

**Deliverables:**
- [ ] Repair ladder wired end-to-end on the live path; shadow evaluator gets a production caller; its `_get_recent_evaluations` entity-type bug fixed en route
- [ ] Element signatures in overlay schema + signature-first healing
- [ ] `to_markdown` compactor mode; markdown as autograph input and optional output artifact
- [ ] `llm_enrichment.py` deleted; one `LLMProviderPort`
- [ ] Contract-test harness with initial fixtures for Maps place/search + one overlay domain
- [ ] Metric: LLM calls per 1000 pages (target: falls as overlays stabilize)

### P5 — API Surface: Verb Taxonomy and Change Tracking

**Goal:** align the FastAPI surface with the Firecrawl verb taxonomy the ecosystem has converged on, and surface the (now-repaired) change detection as a product feature.

**Design:**

| Verb | Route | Semantics |
|---|---|---|
| scrape | `POST /scrape` | Synchronous single-URL fetch+extract (bounded timeout); the CLI `extract` path exposed over HTTP. Returns record(s) + optional markdown/html artifacts |
| crawl | `POST /crawl` → job | Existing `POST /jobs` semantics with `CrawlSpec` (depth, globs); keep `/jobs` as alias for compatibility |
| map | `POST /map` | `SitemapSeeder` + one-hop link discovery; returns URL list fast, no extraction |
| extract | `POST /extract` | HTML/URL + schema in, records out — the existing `/autograph` generalized: generate-or-reuse overlay, then extract |

- All async verbs keep the existing 202 + poll contract (`GET /jobs/{id}`); no new job mechanics.
- **Change tracking:** with P0's post-processor fix live, `ChangeType` (NEW/UPDATED/UNCHANGED) is real. Add `GET /records/{id}/history` (content-hash timeline from the observation store) and a `changes_only=true` filter on `GET /jobs/{id}/records`. changedetection.io-style conditional notifications (only notify on UPDATED matching a rule) ride the existing notifier port with a small `NotificationRule` policy object.
- DTOs stay separate from domain models (remediation W-series rule); each verb gets a request/response schema in an `api/schemas.py` module rather than inline dicts in `main.py`, which also starts breaking up the 572-line `main.py`.

**Patterns:** thin controllers over application services, DTO separation, Policy object (notification rules).

**Deliverables:**
- [ ] Four verbs live with OpenAPI docs; `/jobs` aliased, nothing breaks
- [ ] `changes_only` filter + record history endpoint + rule-gated notifications
- [ ] Integration tests per verb (extends existing `test_api_smoke`)

### P6 — Operations: Scheduling, Autoscaling, Real Telemetry

**Goal:** run unattended. Recurring crawls, workers that size themselves, and SLO endpoints that stop reporting fiction.

**Design:**

- **Scheduling:** `apscheduler` (already in requirements, never imported) driving recurring `CrawlSpec` submissions. `ScheduledCrawl` model: cron expression + spec + enabled flag, persisted in the jobs DB, managed via `POST/GET/DELETE /schedules`. The scheduler runs in the API process lifespan (single instance; multi-instance leader election is out of scope — document the constraint). Each firing goes through the normal outbox → queue path, so scheduled jobs inherit idempotency and observability for free.
- **Autoscaled concurrency:** `AutoscaledWorkerPool` for `worker_scraper`/`worker_processor` — a supervisor coroutine adjusting active consumer tasks between min/max based on the signals the queue already computes (Valkey `INFO memory`, pending depth, host memory via `psutil`). Crawlee's AutoscaledPool shape: scale up when starved and healthy, scale down on memory pressure — integrating with, not duplicating, the queue's existing OOM backpressure.
- **Real SLO metrics:** replace the hardcoded sample metrics in `/health` and `/slo` (`main.py:212-219`, `:515-522`) with reads from the observability counters that P1–P3 now populate. `AutoRollback` and `SLOMonitor` get constructed in the composition root (P0) and fed the same live numbers.
- **Graceful drain:** SIGTERM handler → stop consuming, finish in-flight, checkpoint (Apify actor-migration pattern); makes autoscaling and deploys safe. The queue's ACK semantics already make this near-free.
- Operator dashboard: **non-goal** (see §6). The `/slo` + `/metrics` endpoints plus notification rules are the operator surface for now.

**Patterns:** Supervisor, control loop with hysteresis, lifespan-scoped background services.

**Deliverables:**
- [ ] `ScheduledCrawl` CRUD + lifespan scheduler; recurring crawl demonstrably fires and dedups via idempotency keys
- [ ] Autoscaled pool on both workers with min/max config and scale-event metrics
- [ ] `/health`, `/slo` on live telemetry; hardcoded samples deleted
- [ ] Drain-on-SIGTERM with in-flight completion test

---

## 5. Sequencing and Dependencies

```
P0 (remediation W2/W3/W4 subset — entry gate for everything)
 ├─→ P1 Adaptive fetch  ──→ P3 Sessions/proxies (needs FetcherPort to plumb proxies)
 ├─→ P2 Crawling        ──→ P5 API verbs (map/crawl need frontier + seeder)
 └─→ P4 LLM economics   ──→ P5 (extract verb needs repair ladder)
P1 + P2 + P3 ──→ P6 (autoscaling needs tier metrics; scheduling needs crawl specs)
```

- P1 and P2 are independent after P0 — parallelizable.
- P4 is independent of P1/P2 except for sharing the observation loop — can start immediately after P0.
- P5 is thin (controllers over services) and lands last-but-one deliberately: verbs expose finished capabilities, not aspirations.
- Each phase merges only with its deliverable checkboxes green and the remediation plan's G-gates (import-linter, coverage ≥ 80%, CI green) intact. No phase may introduce a module with zero production callers — the inventory found thirteen of those; this plan retires several and adds none.

**Effort projection (relative):** P0 already in flight per remediation plan · P1 ≈ M · P2 ≈ L · P3 ≈ M · P4 ≈ M · P5 ≈ S · P6 ≈ M.

---

## 6. Explicit Non-Goals

Ruled out after research, not overlooked:

- **CAPTCHA solving integration** (2Captcha/CapMonster) — legal/ToS exposure; detection + session retirement + escalation is the chosen posture.
- **Browser-agent extraction** (browser-use/Skyvern-style LLM-drives-browser) — token cost is antithetical to the overlay economics in P4. Revisit only as a last-rung fallback if the repair ladder's miss rate stays high.
- **Operator dashboard / frontend** — API + notifications suffice at current scale; changedetection.io exists for the watch use case.
- **Postgres migration** — SQLite + WAL holds at current scale; the migration tooling exists when needed. Don't maintain two live stores.
- **Kafka event bus and saga orchestrator** — the outbox + streams path covers current consistency needs; the saga module remains dead code for the remediation plan's W6 to delete.
- **Third fetch tier (Lightpanda)** — beta, incomplete web APIs; reserved a slot in the P1 chain, no build.
- **Distributed multi-host crawling beyond docker-compose scale-out** — consumer-group mechanics (post-P0 naming fix) already give N-replica scaling; cross-host orchestration is premature.

## 7. Risk Register

| # | Risk | Phase | Mitigation |
|---|---|---|---|
| R1 | P0 slips and new capabilities land on the broken path, growing the dead-code graveyard | all | Hard entry gate: no P1+ branch merges before P0 checkboxes; CI check for zero-caller modules (grep-based importer audit, extends remediation G2) |
| R2 | curl_cffi tier bypasses SSRF transport protections | P1 | SSRF validation extracted to a transport-agnostic gate invoked by both tiers; test that Tier-1 requests to RFC1918 targets fail closed |
| R3 | Link discovery + fan-out floods the queue on pathological sites | P2 | Existing Lua fan-out budget + per-crawl seen-set + `max_pages` cap; resilience test with a link-farm fixture |
| R4 | robots.txt fail-closed breaks existing jobs against user-owned targets | P2 | Per-job politeness override flag, default strict; release note |
| R5 | Session/persona binding increases block rate during learning phase | P3 | Exploration fraction capped at 5% (existing policy); rollback = pin factory to static persona list |
| R6 | Overlay self-healing silently degrades extraction quality | P4 | Healed overlays enter as CANDIDATE, never straight to ACTIVE; shadow evaluation + human-approved promotion + AutoRollback all gate the path |
| R7 | Scheduler in API lifespan double-fires when API runs 2 replicas | P6 | Documented single-scheduler constraint + idempotency keys on scheduled submissions make double-fires no-ops |
| R8 | Scope: six phases is quarters of work for one maintainer | all | Phases are independently shippable; P1 alone (fetch tiering) delivers the largest cost win and is the recommended first slice |
