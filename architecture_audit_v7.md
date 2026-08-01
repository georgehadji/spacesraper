# ARCHITECTURAL REAPER — DEEP AUDIT PROTOCOL V7
## Spacescraper — Full Technical Audit
### Audience: Tech Lead | Scope: Full (Parts 1–10) | Date: 2025-07-17

---

## ΠΡΟΑΠΑΙΤΟΥΜΕΝΑ — INPUT DECLARATION

| Available | Input |
|-----------|-------|
| ✅ | **Source code** — `src/` (domain, application, infrastructure, extractors, security), `main.py`, 4 workers, `boot.py`, `spacescraper.py`, `demo_run.py` |
| ✅ | **Dependency manifests** — `requirements.txt`, `requirements-enterprise.txt` |
| ✅ | **Configuration** — `pipeline_config.yaml`, `sources.yaml`, `.env.example`, `config/`, `src/config_settings.py` |
| ✅ | **Docker/Deployment** — `docker-compose.yml`, `docker-compose.enterprise.yml`, `Dockerfile` |
| ✅ | **README / Docs** — `README.md`, `ARCHITECTURE_REMEDIATION_PLAN.md`, `architecture_audit.md`, `SCRAPER_EVOLUTION_PLAN.md`, +5 more planning docs |
| ✅ | **Tests** — 22 test files (unit + integration) covering security, resilience, extraction, repositories, queues |
| ⚠️ | **Logs / metrics** — Code for structured logging + OpenTelemetry exists; no runtime logs provided |
| ❌ | **Architecture diagrams** — Referenced in docs but no diagram files found |
| ❌ | **CI/CD config** — No `.github/`, `.gitlab-ci.yml`, or similar found |
| ❌ | **Interview with developer** — Not available |

> Findings that cannot be verified from source alone are tagged `[ΔΕΔΟΜΕΝΟ ΕΛΛΙΠΕΣ]`.

---

## PRE-ANALYSIS: ΜΕΤΑ-ΕΛΕΓΧΟΙ

### 1. Ανάστροφη αιτιότητα
The architecture remediation plan (`ARCHITECTURE_REMEDIATION_PLAN.md`) self-assesses the project at 5/10. The "cause" appears to be the presence of legacy code paths (old `pipeline.py` alongside new `extraction_pipeline.py`, old `redis_worker.py` alongside new `stream_queue.py`). But the **reverse** may be true: the dual-path architecture is an *effect* of incremental migration rather than neglect. The real root cause is likely **lack of a migration milestone** — no point where old paths were flagged for removal.

### 2. Επιβεβαιωτική προκατάληψη
I actively searched for evidence that the system *is* production-hardened: SLO monitoring, circuit breakers, DLQ, outbox pattern, content-addressed storage, correlation IDs, SSRF protection, input sanitization. These exist and are well-designed. I also searched for the gaps (naive datetimes, missing ownership model, no data retention). Both confirmed.

### 3. Άγνοια άγνοιας
- **PostgreSQL migration path**: `migrate_sqlite_to_postgres.py` exists (22 KB) but its correctness under concurrent load is unknown.
- **Google Maps scraping legality**: No terms-of-service compliance mechanism visible.
- **Valkey cluster behavior**: Code uses Valkey Streams consumer groups; cluster failover behavior is untested.

### 4. Survivorship bias
Bugs that haven't manifested yet:
- `datetime.utcnow()` naive timestamps will break when compared with timezone-aware timestamps from external APIs (AI SDKs, Valkey TTLs).
- `hash()` non-determinism in `OverrideStrategy.build_schema()` will produce different schemas across worker restarts (Python 3.3+ hash randomization).
- `_compute_embedding_cached` in `ai/client.py` always returns `None` — the `@lru_cache` decorator on a method leaks `self`, preventing cache hits. This bug silently disables embedding caching.

### 5. Blast Radius Map

```
┌──────────────┐     ┌──────────────────┐     ┌───────────────────┐
│   main.py    │────▶│   Valkey Streams  │────▶│  worker_scraper   │
│  (FastAPI)   │     │  (jobs_stream,    │     │  (Playwright)     │
│              │     │   raw_payloads,   │     │                   │
│  /jobs POST  │     │   discovery)      │     │  SmartCrawler →   │
│  /jobs GET   │     │                   │     │  ArtifactStore    │
│  /records    │     │  Single point of  │     │                   │
│  /overlays   │     │  FAILURE for all  │     │  Crash → stale    │
│  /exports    │     │  inter-worker     │     │  jobs in RUNNING  │
│              │     │  communication    │     │  (no heartbeat)   │
└──────────────┘     └────────┬─────────┘     └────────┬──────────┘
                              │                        │
                              ▼                        ▼
                     ┌──────────────────┐     ┌───────────────────┐
                     │ worker_processor │     │  worker_reporter   │
                     │                  │     │                   │
                     │ ExtractionPipe-  │     │  Slack/Webhook/   │
                     │ line → AI enrich │     │  CSV/JSON export  │
                     │                  │     │                   │
                     │ Crash → records  │     │  Crash → missed   │
                     │ never extracted  │     │  notifications    │
                     └────────┬─────────┘     └───────────────────┘
                              │
                              ▼
                     ┌──────────────────┐
                     │     SQLite       │
                     │  (or PostgreSQL) │
                     │                  │
                     │  Single DB —     │
                     │  no read replicas│
                     │  no failover     │
                     └──────────────────┘
```

**Critical SPOFs:**
1. **Valkey/Redis** — all inter-service communication. If it goes down, the entire pipeline halts.
2. **SQLite/PostgreSQL** — single-instance. No read replicas, no failover.
3. **No job heartbeat** — scraper crash leaves jobs in RUNNING state indefinitely.

---

## ΜΕΡΟΣ 1: ΧΡΟΝΙΚΗ ΣΥΜΒΑΤΟΤΗΤΑ

**Depth Budget: 4 findings**

### Finding 1.1 — Naive datetimes everywhere (P1)

| Field | Value |
|-------|-------|
| **Location** | `src/domain/models.py:54,55,65,73,101,130,155,199,200,225,238,258,292,306,335,336,338,350,413,414,446,449` — 23 occurrences of `datetime.utcnow()` |
| **Scenario** | Every timestamp in the system (created_at, updated_at, finished_at, last_seen, timestamp fields on all domain models) uses `datetime.utcnow()` which returns a **naive** datetime (no timezone attribute). |
| **Failure** | When these are compared with timezone-aware datetimes from external systems (OpenAI SDK responses, Valkey TTL expiry, PostgreSQL `TIMESTAMPTZ` columns), Python raises `TypeError: can't compare offset-naive and offset-aware datetimes`. DST transitions are invisible — there is no way to know if a timestamp was recorded during BST or GMT. |
| **Severity** | **P1** — will break in production when integrated with timezone-aware systems |
| **Confidence** | **HIGH** — verified in code across all models |

### Finding 1.2 — API key expiration without refresh mechanism (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/auth_middleware.py:244-248` (expiry check), `src/auth_middleware.py:41-50` (ApiKey model) |
| **Scenario** | API keys have an `expires_at` field checked against `datetime.utcnow()`. When a key expires, the call returns HTTP 403. There is **no refresh mechanism**, **no warning before expiry**, and **no way to rotate keys without downtime**. |
| **Failure** | Production API clients suddenly receive 403 errors with no advance notice. Keys must be manually regenerated and redistributed — a manual, error-prone process. |
| **Severity** | **P2** — will cause disruption at scale but has workarounds |
| **Confidence** | **HIGH** — verified in code |

### Finding 1.3 — No cron overlap protection for scheduled tasks (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/infrastructure/repositories/observation_repository.py` — profile update logic runs hourly; `src/application/strategy_selector.py` — strategy refresh timer |
| **Scenario** | Scheduled operations (domain profile refresh, strategy evaluation) use simple interval timers. If a refresh takes longer than the interval, two instances run concurrently. There is no lock, no overlap guard, and no "skip if running" check. |
| **Failure** | Concurrent profile updates produce race conditions on `DomainProfile` data. Two evaluations may produce conflicting strategy assignments. |
| **Severity** | **P2** — degrades quality under load but not catastrophic |
| **Confidence** | **MEDIUM** — inferred from timer patterns in code; runtime behavior not observed |

### Finding 1.4 — External API timeout inconsistency (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/infrastructure/ai/client.py:85` (AI timeout: 30s), `src/infrastructure/http_client.py` (HTTP timeout: configurable), `src/infrastructure/browser/engine.py` (Playwright timeout: implicit 30s default), `src/infrastructure/providers/enrichment_provider.py:81` (enrichment retry: 1s→2s→4s backoff) |
| **Scenario** | Different external calls use different timeout strategies: AI client has 30s + circuit breaker; HTTP client has configurable timeout; Playwright uses built-in defaults; enrichment provider has exponential backoff. There is no **global timeout budget** — a single job could block for 30s (AI) + 30s (browser) + 8s (enrichment retries) = 68s without the caller knowing the total timeout. |
| **Failure** | The pipeline_config.yaml SLA target is < 300s per job, but there is no enforcement of cumulative timeouts. A job could silently exceed SLA with no alert. |
| **Severity** | **P2** — no SLA violation alerting exists |
| **Confidence** | **HIGH** — verified across multiple infrastructure files |

### Part 1 Summary

| Finding | Location | Scenario | Severity | Confidence |
|---------|----------|----------|----------|------------|
| 1.1 Naive datetimes | `models.py` (23 occurrences) | `datetime.utcnow()` returns naive datetimes | P1 | HIGH |
| 1.2 No key refresh | `auth_middleware.py:244-248` | Keys expire with no warning/rotation | P2 | HIGH |
| 1.3 No cron overlap guard | `strategy_selector.py`, `observation_repository.py` | Concurrent scheduled runs | P2 | MEDIUM |
| 1.4 Timeout inconsistency | `ai/client.py`, `http_client.py`, `browser/engine.py` | No cumulative timeout budget | P2 | HIGH |

---

## ΜΕΡΟΣ 2: ΣΧΕΔΙΑΣΤΙΚΕΣ ΑΠΟΦΑΣΕΙΣ ΠΟΥ ΜΟΙΑΖΟΥΝ ΜΕ BUGS

**Depth Budget: 6 findings**

### Finding 2.1 — Outbox pattern guarantees at-least-once, not exactly-once (P1)

| Field | Value |
|-------|-------|
| **Location** | `src/infrastructure/outbox_relay.py`, `src/infrastructure/queues/stream_queue.py` |
| **Scenario** | The outbox pattern writes events atomically with the job transaction, then relays them to Valkey Streams. Consumer groups provide ACK. However: (1) The outbox relay has no transactional boundary with the Valkey push — if the relay crashes after pushing but before marking delivered, the event is resent. (2) Consumer group ACK is acknowledged *after* processing, but if the consumer crashes *during* processing and before ACK, the message is redelivered. |
| **Failure** | Duplicate processing. The system has partial protection via `identity_hash` for records and `message_id` + `event_id` for dedup, but these are **not enforced at the consumer level** — it's up to each consumer to check. `worker_processor.py` does **not** check `message_id` dedup before processing. |
| **Severity** | **P1** — duplicate extraction under crash-recovery is likely under normal ops |
| **Confidence** | **HIGH** — verified in stream_queue and worker code |

### Finding 2.2 — `hash()` non-determinism in OverrideStrategy (P1)

| Field | Value |
|-------|-------|
| **Location** | `src/extractors/strategies/override.py:39-61` (`build_schema` method) |
| **Scenario** | `build_schema()` uses Python's built-in `hash(tuple(...))` to generate a deterministic schema ID. Since Python 3.3, `hash()` is randomized per process (PYTHONHASHSEED). The same input produces different hash values across worker restarts, processes, or containers. This means the same user mapping generates a different schema ID each time. |
| **Failure** | Schema IDs are non-deterministic across deployments. Schema lookup by ID fails after worker restart. Overlays referencing these schema IDs become orphaned. |
| **Severity** | **P1** — data integrity issue; schemas become unretrievable after restart |
| **Confidence** | **HIGH** — verified in code; `hash()` randomization is documented Python behavior |

### Finding 2.3 — No backpressure between scraper and processor (P2)

| Field | Value |
|-------|-------|
| **Location** | `worker_scraper.py` (producer), `worker_processor.py` (consumer), `src/infrastructure/rate_limiter.py` |
| **Scenario** | The scraper worker can produce `RawScrapePayload` messages much faster than the processor can consume them (scraping is I/O-bound; processing includes AI calls). The Valkey Stream sits between them, but there is **no consumer lag monitoring** and **no backpressure signal** from processor to scraper. If the processor falls behind, the stream grows unboundedly. `pipeline_config.yaml:57` mentions `max_fanout` for discovery but not for the main pipeline. |
| **Failure** | Under high load, Valkey memory fills with unprocessed payloads. Redis OOM → eviction or crash → data loss. |
| **Severity** | **P2** — requires sustained high load to trigger |
| **Confidence** | **HIGH** — verified in worker and rate_limiter code |

### Finding 2.4 — Duck-typing extraction strategies (no interface enforcement) (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/extractors/base_extractor.py` (BaseExtractionStrategy), `src/extractors/strategies/*.py` (concrete strategies) |
| **Scenario** | `BaseExtractionStrategy` defines the protocol, but only `UniversalExtractionStrategy` inherits from it. `GoogleMapsStrategy`, `GoogleMapsPlaceStrategy`, `GenericStrategy`, and `OverrideStrategy` do **not** inherit from the base class. They rely on duck-typing — matching `extract()` method signatures. |
| **Failure** | No compiler/type-checker enforcement. If a strategy changes its `extract()` signature, it silently breaks at runtime with an opaque `AttributeError`. New strategies may omit required methods. |
| **Severity** | **P2** — maintainability risk, not runtime under normal conditions |
| **Confidence** | **HIGH** — verified across all strategy files |

### Finding 2.5 — Graceful degradation: AI circuit breaker works, but no fallback for Playwright (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/infrastructure/ai/client.py:100-107` (circuit breaker), `src/infrastructure/browser/pool.py` (browser pool), `src/smart_crawler.py` (HTTP-first crawl) |
| **Scenario** | The AI client has a circuit breaker with exponential backoff. The HTTP crawler uses ETag/If-Modified-Since for cache validation. But if Playwright crashes or the browser pool is exhausted, the **entire scraping path fails** — there is no fallback to raw HTTP-only extraction (even though `smart_crawler.py` already does HTTP HEAD checks, it delegates to Playwright for full fetches). |
| **Failure** | Browser pool exhaustion → all scraping jobs fail with no degraded mode. The `turbo_guard` mechanism (`test_resilience_turbo_guard.py`) exists for domain-specific optimization but is not a general fallback. |
| **Severity** | **P2** — happens under resource pressure |
| **Confidence** | **HIGH** — verified in browser pool and smart_crawler code |

### Finding 2.6 — Idempotency: OverrideStrategy POST is not idempotent (P2)

| Field | Value |
|-------|-------|
| **Location** | `main.py` (POST /overlays endpoint — assumed from API patterns), `src/infrastructure/repositories/overlay_repository.py` |
| **Scenario** | `OverlayRepository.update_overlay_state()` is documented as idempotent (ports.py:143), and `OutboxRepository` has idempotency keys (`event_id`, `message_id`). However, there is no deduplication key on overlay **creation**. POST the same overlay twice → two overlays. The job creation path has the same gap — `create_job` raises on duplicate `job_id`, but the caller must generate a unique `job_id` client-side. If the client retries a POST without preserving the `job_id`, a duplicate job is created. |
| **Failure** | Network retry from client → duplicate jobs/overlays created. The API does not provide an idempotency-key header pattern. |
| **Severity** | **P2** — requires client retry + missing idempotency key |
| **Confidence** | **HIGH** — verified in ports.py and repository code |

### Part 2 Summary

| Operation | Decision | Documented | Risk if Violated | Severity | Confidence |
|-----------|----------|------------|-------------------|----------|------------|
| Outbox → Valkey relay | At-least-once, no consumer dedup check | No | Duplicate extraction | P1 | HIGH |
| Override schema hash | `hash()` — non-deterministic | No | Schema IDs break on restart | P1 | HIGH |
| Scraper → Processor flow | No backpressure signal | No | Valkey OOM, data loss | P2 | HIGH |
| Strategy interface | Duck-typing, no ABC enforcement | No | Runtime breakage on refactor | P2 | HIGH |
| Playwright failure | No HTTP-only fallback | No | All jobs fail on pool exhaustion | P2 | HIGH |
| POST /jobs, /overlays | No idempotency-key pattern | No | Duplicate entities on retry | P2 | HIGH |

---

## ΜΕΡΟΣ 3: ΠΑΡΑΤΗΡΗΣΙΜΟΤΗΤΑ & ΚΟΣΤΟΣ

**Depth Budget: 5 findings**

### Finding 3.1 — Correlation IDs don't propagate to worker contexts (P1)

| Field | Value |
|-------|-------|
| **Location** | `src/infrastructure/middleware/correlation.py:11-42` (middleware), `worker_scraper.py`, `worker_processor.py`, `worker_reporter.py` (workers — no correlation ID import) |
| **Scenario** | The correlation middleware sets a `ContextVar` with the request ID for HTTP requests. But **none of the three workers import or use `get_request_id()`**. Workers consume from Valkey Streams, not HTTP — the correlation ID is never propagated from the API request through the queue into the worker. End-to-end tracing is broken: you can trace the API call, and you can trace the worker, but you cannot connect them. |
| **Failure** | "Find the job that came from this API request" is impossible without manually correlating timestamps. Incident response time increases dramatically. |
| **Severity** | **P1** — breaks end-to-end observability for the core use case |
| **Confidence** | **HIGH** — verified via grep: no worker imports `get_request_id` |

### Finding 3.2 — Business metrics exist but no anomaly alerting (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/infrastructure/slo_monitor.py` (SLO evaluation), `src/infrastructure/middleware/observability.py` (OpenTelemetry metrics), `src/config_settings.py` (SLO thresholds) |
| **Scenario** | The system has 6 SLOs with warning/critical thresholds and AutoRollback on regression. OpenTelemetry exports metrics to OTLP. However, there is **no anomaly detection** on business KPIs — the SLO thresholds are static. A gradual degradation (e.g., extraction success rate dropping from 98% to 85% over 4 hours) won't trigger until it crosses the fixed critical threshold, by which time many jobs have already failed. |
| **Failure** | Slow degradation goes undetected. The SLO `AutoRollback` only triggers on threshold crossing, not on trend. |
| **Severity** | **P2** — requires gradual degradation to manifest |
| **Confidence** | **HIGH** — verified in SLO monitor code |

### Finding 3.3 — No dead man's switch for scheduled tasks (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/application/strategy_selector.py`, `src/infrastructure/repositories/observation_repository.py` |
| **Scenario** | Domain profile updates and strategy evaluations run on timers. There is no external heartbeat or dead man's switch — if the process silently dies, no alert fires. The system simply stops updating profiles. Extraction quality degrades as profiles become stale, but there's no notification. |
| **Failure** | Strategy profiles go stale → extraction quality degrades → no alert → discovered only when someone notices data quality issues. |
| **Severity** | **P2** — silent failure mode |
| **Confidence** | **HIGH** — no heartbeat mechanism found in codebase |

### Finding 3.4 — No cost monitoring or per-operation cost tracking (P3)

| Field | Value |
|-------|-------|
| **Location** | Entire codebase — `[ΔΕΔΟΜΕΝΟ ΕΛΛΙΠΕΣ]` for cloud billing data |
| **Scenario** | Every AI enrichment call costs money (OpenAI/Gemini API). Every Playwright browser session consumes CPU/memory. There is **no cost attribution** — no tracking of tokens consumed per job, no cost-per-extraction metric, no budget alerting. The `pipeline_config.yaml` has no cost-related configuration. |
| **Failure** | A bug causing retry storms on AI calls could generate a large API bill with no warning. Cost optimization decisions (e.g., "should we cache more?") have no data to support them. |
| **Severity** | **P3** — operational efficiency gap, not reliability |
| **Confidence** | **HIGH** — no cost tracking imports or metrics found |

### Finding 3.5 — Structured logging is configured but not consistently used (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/infrastructure/logger_config.py` (structured JSON logger), `src/infrastructure/ai/client.py:112` (uses `logger.error`), `src/extractors/strategies/google_maps.py:301` (uses `logger.debug`), `src/infrastructure/proxies/manager.py:40` (uses `logger.debug`) |
| **Scenario** | `logger_config.py` sets up `python-json-logger` with structured JSON output. Most infrastructure code uses it. However, extractor code uses free-text debug logging (`logger.debug(f"field mapping failed: ...")`) without structured fields. The correlation ID is not attached to log entries (see Finding 3.1). |
| **Failure** | Log queries like "show me all errors for job X" are impossible because job ID and correlation ID are not consistently included as structured fields. |
| **Severity** | **P2** — debuggability gap |
| **Confidence** | **HIGH** — verified across multiple source files |

### Part 3 Summary

| Category (Pillar) | Current State | Gap | Impact | Severity | Confidence |
|-------------------|---------------|-----|--------|----------|------------|
| Traces | Correlation middleware exists | IDs don't cross API→worker boundary | Cannot trace jobs end-to-end | P1 | HIGH |
| Metrics | 6 SLOs + OpenTelemetry | No anomaly/trend detection | Gradual degradation missed | P2 | HIGH |
| Alerting | SLO thresholds + AutoRollback | No dead man's switch for timers | Silent process death | P2 | HIGH |
| Cost | No cost tracking | No per-job token/budget metrics | Uncontrolled AI API spend | P3 | HIGH |
| Logs | Structured JSON configured | Correlation/job IDs not in log context | Cannot query by job | P2 | HIGH |

---

## ΜΕΡΟΣ 4: ΑΝΘΡΩΠΙΝΟΙ ΠΑΡΑΓΟΝΤΕΣ

**Depth Budget: 4 findings**

### Finding 4.1 — Error messages: mixed quality across layers (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/domain/exceptions.py:5-34` (structured errors), `src/extractors/universal_strategy.py:73,182` (silent error swallowing), `src/extractors/strategies/google_maps.py:301` (all-field loss on single error) |
| **Scenario** | The domain exception hierarchy is well-designed: every exception carries `code` (str) and `details` (dict). But in the extraction layer: (1) `JSONDecodeError` in JSON-LD parsing is silently caught and discarded (universal_strategy.py:73). (2) Overlay extraction errors are logged at `debug` level and swallowed (universal_strategy.py:182). (3) Google Maps `_entry_to_data` wraps all 17+ field extractions in a single `try/except` — one field failure loses ALL fields with no per-field error detail. |
| **Failure** | A developer debugging "why is this field empty?" has no error signal — the error was silently swallowed. Debugging requires adding temporary logging or reproducing with a debugger. |
| **Severity** | **P2** — operational toil increase |
| **Confidence** | **HIGH** — verified at each cited line |

### Finding 4.2 — Documentation: strong planning, weak runbooks (P2)

| Field | Value |
|-------|-------|
| **Location** | `README.md` (5 KB — good), `ARCHITECTURE_REMEDIATION_PLAN.md` (16 KB — comprehensive), `architecture_audit.md`, `SCRAPER_EVOLUTION_PLAN.md`, `USER_GUIDE.md`, `DEPLOYMENT.md`, `QUICKSTART_LAPTOP.md` |
| **Scenario** | The project has excellent architectural documentation and evolution plans. However, there are **no operational runbooks**: no "what to do when the scraper is down" guide, no incident response procedures, no troubleshooting flowcharts. The 6 SLOs have thresholds but no linked runbooks for what to do when they fire. |
| **Failure** | 3AM alert fires → on-call engineer has no playbook. Time-to-resolution depends entirely on individual familiarity with the codebase. |
| **Severity** | **P2** — incident response gap |
| **Confidence** | **HIGH** — no runbook files found in repo |

### Finding 4.3 — Bus factor: Google Maps indices are critical single-person knowledge (P1)

| Field | Value |
|-------|-------|
| **Location** | `src/extractors/strategies/google_maps.py:198-304` (`_entry_to_data`), `src/extractors/strategies/google_maps.py:322-365` (`url_to_grid_cells`) |
| **Scenario** | The Google Maps strategy contains ~20 hardcoded array indices mapping Google's internal JSON structure to business fields (e.g., `entry[178][0][0]` for reviews, `arr[14]` for place ID). This knowledge was ported from a Go reference implementation and lives in a single function. If Google changes their data format, or if the Maps strategy breaks, **only someone who understands these indices can fix it**. There is no external documentation of what each index means. |
| **Failure** | Google changes their internal JSON → Maps extraction breaks → no one knows how to remap the indices → feature is dead until reverse-engineering is repeated. |
| **Severity** | **P1** — critical extraction path depends on undocumented magic numbers |
| **Confidence** | **HIGH** — verified; 23 tests partially mitigate but don't document the mapping |

### Finding 4.4 — Onboarding: good dev setup, weak code navigation (P3)

| Field | Value |
|-------|-------|
| **Location** | `README.md`, `QUICKSTART_LAPTOP.md`, `docker-compose.yml` |
| **Scenario** | The project has good onboarding docs: one-command Docker setup, clear environment variable template, documented API endpoints. But there is **no architecture decision record (ADR)** explaining *why* certain choices were made (e.g., why Valkey Streams over Kafka for the default deployment, why SQLite over PostgreSQL for lightweight mode, why the dual pipeline.py/extraction_pipeline.py split). New developers can run the code but cannot understand the design rationale. |
| **Failure** | New developer makes a change that violates an undocumented design constraint → PR rejected → frustration → slower onboarding. |
| **Severity** | **P3** — onboarding friction |
| **Confidence** | **HIGH** — no ADR directory or files found |

### Part 4 Summary

| Area | Gap | Impact | Severity | Confidence |
|------|-----|--------|----------|------------|
| Error Messages | Silent error swallowing in extraction layer | Debugging requires code instrumentation | P2 | HIGH |
| Documentation | No operational runbooks | 3AM incident response depends on individual knowledge | P2 | HIGH |
| Bus Factor | Google Maps indices undocumented magic numbers | Feature death if format changes | P1 | HIGH |
| Onboarding | No ADRs for design rationale | New devs can't understand "why" | P3 | HIGH |

---

## ΜΕΡΟΣ 5: ΑΣΦΑΛΕΙΑ ΠΕΡΑ ΑΠΟ ΤΟ ΠΡΟΦΑΝΕΣ

**Depth Budget: 8 findings**

### Finding 5.1 — Mass assignment: Pydantic v2 provides whitelist by default (OK)

| Field | Value |
|-------|-------|
| **Location** | `src/domain/models.py` — all Pydantic models use `model_config = {"extra": "forbid"}` (Pydantic v2 default with `BaseModel`) |
| **Scenario** | All domain models inherit from Pydantic `BaseModel`, which by default rejects extra fields. The API endpoints use these models for request validation. Extra JSON fields in a POST body are rejected. |
| **Status** | **No finding — this is correct.** Pydantic v2 provides field whitelisting by default. |
| **Severity** | N/A |
| **Confidence** | **HIGH** |

### Finding 5.2 — IDOR: No resource ownership model (P1 for multi-tenant)

| Field | Value |
|-------|-------|
| **Location** | `src/domain/models.py` (no owner_id on Job, Record, Overlay), `src/domain/ports.py` (no user_id parameter on any repository method), `main.py` (API endpoints have no per-resource authz check) |
| **Scenario** | The system has no concept of resource ownership. There is no `owner_id`, `tenant_id`, or `user_id` on any domain model. The repository protocols have no user-scoping parameter. Any valid API key can access any job, record, or overlay. If the system is single-tenant by design, this is acceptable. But the architecture supports multiple API keys with different tiers — suggesting multi-tenancy is planned. |
| **Failure** | In a multi-tenant deployment, API key A can read/delete API key B's jobs and records. This is a classic IDOR vulnerability if multi-tenancy is expected. |
| **Severity** | **P1** (if multi-tenant) / **P3** (if intentionally single-tenant) |
| **Confidence** | **HIGH** — verified across models, ports, and auth middleware. `[ΔΕΔΟΜΕΝΟ ΕΛΛΙΠΕΣ]` — need confirmation whether multi-tenancy is planned. |

### Finding 5.3 — TOCTOU: SSRF guard admits DNS rebinding gap (P1)

| Field | Value |
|-------|-------|
| **Location** | `src/security/ssrf_guard.py:31-34` (documented limitation), `src/security/ssrf_guard.py:35-80` (`validate_outbound_url`) |
| **Scenario** | The SSRF guard resolves the hostname to an IP and checks if it's private — **then** the HTTP client makes the actual request, potentially resolving the hostname again. A DNS rebinding attack can return a public IP during validation and a private IP (e.g., 169.254.169.254 AWS metadata) during the actual connection. The code acknowledges this on lines 31-34. |
| **Failure** | Attacker registers `evil.com` → DNS responds with `1.2.3.4` (public) during SSRF check → DNS TTL expires → subsequent resolution returns `169.254.169.254` → HTTP client connects to AWS metadata endpoint → IAM credentials leaked. |
| **Severity** | **P1** — exploitable on cloud deployments with IMDSv1 |
| **Confidence** | **HIGH** — documented in code; well-known attack vector |

### Finding 5.4 — Side-channel: API key validation timing leak (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/auth_middleware.py:195-256` (`verify_api_key`) |
| **Scenario** | The API key validation iterates through `_keys_by_hash` comparing SHA-256 hashes. Python's dict lookup is O(1) average, but the key hash computation via `hashlib.sha256(key.encode()).hexdigest()` has timing proportional to key length. More critically, the function does an `is_active` check (line 139) and `expires_at` check (line 244) **after** finding the key hash — an attacker can distinguish "valid key, expired" from "invalid key" by response timing or status code (403 for expired vs 401 for invalid). |
| **Failure** | Attacker enumerates valid keys by observing 401 vs 403 response codes. |
| **Severity** | **P2** — information disclosure; requires active probing |
| **Confidence** | **MEDIUM** — inferred from code structure; would need timing measurement to confirm |

### Finding 5.5 — Secrets: DEMO_API_KEY backdoor in production risk (P1)

| Field | Value |
|-------|-------|
| **Location** | `src/auth_middleware.py:219-228` (demo key bypass), `.env` (contains `DEMO_API_KEY` set) |
| **Scenario** | In development mode, the auth middleware accepts a hardcoded `DEMO_API_KEY` from environment variables, bypassing all tier/rate-limit checks. The `.env` file in the repository has this key set. The `ENVIRONMENT` env var controls whether this bypass is active. If `ENVIRONMENT` is accidentally left as `development` in a production deployment, the demo key provides **unauthenticated, unlimited access**. |
| **Failure** | Production deployment with `ENVIRONMENT=development` → demo key bypasses all auth → full API access with no rate limiting, no audit trail tied to a real key. |
| **Severity** | **P1** — configuration error leads to complete auth bypass |
| **Confidence** | **HIGH** — verified in auth_middleware.py and .env |

### Finding 5.6 — Secrets: API keys stored in-memory only, lost on restart (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/auth_middleware.py:84` (`_keys_by_hash: dict`), `src/auth_middleware.py:86` (keys loaded from env var `API_KEYS_JSON`) |
| **Scenario** | All API keys are loaded from a JSON environment variable at startup and stored in an in-memory dict. There is no persistent key store, no database backing, no key generation audit trail. On restart, all keys must be re-provided via the environment variable. The `ApiKeyGenerator` class (line 267-288) is a CLI tool but only prints to stdout — it doesn't persist anywhere. |
| **Failure** | Restart → all keys lost → all clients receive 401 until keys are manually regenerated and redistributed. No way to audit "who generated which key when." |
| **Severity** | **P2** — operational disruption on restart; no audit trail |
| **Confidence** | **HIGH** — verified in auth_middleware.py |

### Finding 5.7 — Sensitive data sent to AI with no field-level redaction (P1)

| Field | Value |
|-------|-------|
| **Location** | `src/infrastructure/ai/client.py:63-64` (`json.dumps(data, indent=2, default=str)` sent to Gemini), `src/infrastructure/ai/client.py:119` (raw HTML chunk sent), `src/infrastructure/ai/client.py:161` (HTML sample for overlay generation), `src/infrastructure/ai/client.py:188` (opportunity data), `src/infrastructure/providers/enrichment_provider.py:63-64` (same pattern) |
| **Scenario** | Extracted data — potentially containing PII, phone numbers, email addresses, physical addresses — is sent verbatim to OpenAI/Gemini APIs for enrichment. There is **no field-level redaction** before transmission. The `input_sanitizer.py` has prompt injection filtering but no PII redaction. Phone numbers from Google Maps (e.g., `google_maps.py:297-299`) and reviews with author names (`google_maps_place.py:132-138`) flow directly into AI prompts. |
| **Failure** | PII from scraped websites is transmitted to third-party AI providers → GDPR/privacy compliance violation → data processing agreement breach. |
| **Severity** | **P1** — regulatory/compliance risk for any PII-containing scrapes |
| **Confidence** | **HIGH** — verified in ai/client.py and enrichment_provider.py |

### Finding 5.8 — Docker: runs as root, no security hardening (P2)

| Field | Value |
|-------|-------|
| **Location** | `Dockerfile:1-30` (full file, 1241 B) |
| **Scenario** | The Dockerfile uses `python:3.11-slim`, installs Chromium, and runs the application. It does **not** create a non-root user, does **not** drop capabilities, does **not** set `readOnlyRootFilesystem`, and does **not** use multi-stage builds. The container runs as root. If the Playwright browser is compromised (e.g., via a malicious page exploiting a Chromium vulnerability), the attacker has root in the container. |
| **Failure** | Browser exploit → container root access → access to environment variables (API keys, database URLs) → potential lateral movement to Redis/PostgreSQL. |
| **Severity** | **P2** — defense-in-depth gap |
| **Confidence** | **HIGH** — verified in Dockerfile |

### Part 5 Summary

| Finding | STRIDE Category | Attack Vector | Current Control | Gap | Severity | Confidence |
|---------|----------------|---------------|-----------------|-----|----------|------------|
| 5.2 No ownership model | Information Disclosure / Elevation | Valid API key → access all resources | API key auth (authN) | No per-resource authZ | P1* | HIGH |
| 5.3 DNS rebinding SSRF | Elevation of Privilege | DNS rebinding between check and connect | IP blocklist check | TOCTOU between resolve and connect | P1 | HIGH |
| 5.5 Demo key backdoor | Elevation of Privilege | ENVIRONMENT=development in prod | Environment check | Single env var controls auth bypass | P1 | HIGH |
| 5.7 PII to AI providers | Information Disclosure | Scraped PII → OpenAI/Gemini prompts | Prompt injection filtering only | No PII redaction before AI transmission | P1 | HIGH |
| 5.4 Key validation timing | Information Disclosure | Observe 401 vs 403 timing/codes | None | Valid key enumeration possible | P2 | MEDIUM |
| 5.6 Keys in-memory only | Denial of Service | Restart → all keys lost | Env var loading | No persistent key store | P2 | HIGH |
| 5.8 Docker root | Elevation of Privilege | Browser CVE → container root | None | No non-root user, no capability drop | P2 | HIGH |

*\*P1 if multi-tenant; P3 if intentionally single-tenant. Needs confirmation.*

---

## ΜΕΡΟΣ 6: ΔΙΑΧΕΙΡΙΣΗ ΔΕΔΟΜΕΝΩΝ

**Depth Budget: 5 findings**

### Finding 6.1 — No delete operations exist anywhere (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/domain/ports.py` (no delete methods on any repository), `src/infrastructure/repositories/job_repository.py`, `record_repository.py` |
| **Scenario** | The system has no concept of deletion — not soft delete, not hard delete. There is no `delete_job`, `delete_record`, `archive_record`, `soft_delete`, `deleted_at` field, or `is_deleted` flag anywhere in the codebase. Data is immutable once created. `JobState.CANCELLED` and `OverlayState.DISABLED`/`RETIRED` are domain lifecycle states, not deletion. |
| **Failure** | GDPR "right to erasure" (Article 17) cannot be fulfilled. If the system scrapes personal data, there is no mechanism to delete it. |
| **Severity** | **P2** — compliance gap if PII is scraped |
| **Confidence** | **HIGH** — verified across all models, ports, and repositories |

### Finding 6.2 — No data retention policy (P2)

| Field | Value |
|-------|-------|
| **Location** | Entire codebase — `[ΔΕΔΟΜΕΝΟ ΕΛΛΙΠΕΣ]` for organizational policy docs |
| **Scenario** | There is no data retention configuration, no TTL on stored records, no automatic cleanup of old jobs/artifacts, and no retention policy documentation. The artifact store (`artifact_store.py`) keeps content-addressed files indefinitely. SQLite/PostgreSQL tables grow without bound. |
| **Failure** | Disk fills up over months of operation. No automatic mechanism to prune old data. GDPR "storage limitation" principle (Article 5(1)(e)) is not implemented. |
| **Severity** | **P2** — operational + compliance risk |
| **Confidence** | **HIGH** — no retention/TTL logic found |

### Finding 6.3 — No cascading deletes (safe default, but no cleanup path) (P3)

| Field | Value |
|-------|-------|
| **Location** | `src/database_models.py` (SQLAlchemy ORM models — no `ondelete` cascade), `src/infrastructure/repositories/` (no delete methods) |
| **Scenario** | Since there are no delete operations (Finding 6.1), cascading deletes are not applicable. However, if deletion is added in the future, the relationship between `Job` → `JobAttempt` (one-to-many), `Job` → `ExtractedRecord` (one-to-many), and `ExtractionSchema` → `ExtractionOverlay` (one-to-many) will need explicit cascade behavior defined. Currently, foreign key relationships exist only in the SQLAlchemy ORM models (for PostgreSQL) and not in the SQLite path. |
| **Failure** | Future delete implementation without cascade → orphaned records. |
| **Severity** | **P3** — future risk, not current |
| **Confidence** | **MEDIUM** — inferred from current code structure |

### Finding 6.4 — Migration script has no rollback capability (P2)

| Field | Value |
|-------|-------|
| **Location** | `migrate_sqlite_to_postgres.py` (22 KB) |
| **Scenario** | The SQLite → PostgreSQL migration script is a one-way script. There is no `--dry-run` flag, no rollback mechanism, no verification step before committing. The script reads all data from SQLite and writes to PostgreSQL. If it fails midway, the PostgreSQL database is in an inconsistent state (partial data). |
| **Failure** | Migration fails at 60% → PostgreSQL has partial data → no way to roll back → manual cleanup required. Zero-downtime migration is not addressed. |
| **Severity** | **P2** — data integrity risk during migration |
| **Confidence** | **HIGH** — verified by reading migration script structure |

### Finding 6.5 — No data classification or sensitivity labeling (P3)

| Field | Value |
|-------|-------|
| **Location** | `src/domain/models.py` — `ExtractedRecord.data` is `Dict[str, Any]` with no field-level metadata |
| **Scenario** | All extracted data is treated uniformly. There is no mechanism to mark fields as PII, sensitive, or public. The enrichment pipeline cannot distinguish between "safe to send to AI" and "contains personal data." Finding 5.7 (PII to AI) is exacerbated by this gap. |
| **Failure** | Cannot implement selective AI enrichment → either send everything (privacy risk) or send nothing (loss of functionality). |
| **Severity** | **P3** — design gap that amplifies other risks |
| **Confidence** | **HIGH** — verified in models and pipeline code |

### Part 6 Summary

| Area | Current Behavior | Risk | Severity | Confidence |
|------|-----------------|------|----------|------------|
| Delete operations | None exist — soft or hard | GDPR non-compliance | P2 | HIGH |
| Data retention | No policy, no TTL, no auto-cleanup | Unbounded storage growth | P2 | HIGH |
| Cascading deletes | N/A (no deletes exist) | Orphaned records if deletes added | P3 | MEDIUM |
| Migration safety | One-way, no rollback, no dry-run | Partial data on failure | P2 | HIGH |
| Data classification | No sensitivity labeling | Amplifies PII-to-AI risk | P3 | HIGH |

---

## ΜΕΡΟΣ 7: ΑΝΤΙΜΕΤΩΠΙΣΗ ΑΣΤΟΧΙΑΣ ΣΕ ΒΑΘΟΣ

**Depth Budget: 6 findings**

### Finding 7.1 — No consumer deduplication despite idempotency keys (P1)

| Field | Value |
|-------|-------|
| **Location** | `worker_processor.py` (no message_id dedup check), `src/infrastructure/queues/stream_queue.py:90-140` (consumer with ACK but no dedup) |
| **Scenario** | `QueueMessage` has a `message_id` field documented as "Unique UUID for deduplication." The outbox relay has `event_id` as an idempotency key. But the stream consumer processes messages without checking if the message was already processed. If a message is redelivered (consumer crash before ACK, or at-least-once delivery), it is processed again. `ExtractedRecord.identity_hash` provides partial protection against duplicate records, but the entire extraction pipeline re-runs unnecessarily. |
| **Failure** | Crash recovery → message redelivered → duplicate extraction → wasted AI API calls → duplicate records if identity_hash collision (unlikely but possible). |
| **Severity** | **P1** — wastes resources and risks duplicate data under normal failure-recovery |
| **Confidence** | **HIGH** — verified in worker_processor.py and stream_queue.py |

### Finding 7.2 — DLQ exists but is not monitored (P1)

| Field | Value |
|-------|-------|
| **Location** | `src/infrastructure/queues/stream_queue.py` (DLQ push logic), `pipeline_config.yaml:36` (DLQ quarantine mention), `tests/test_stream_queue.py:64-95` (DLQ tests) |
| **Scenario** | The stream queue has a Dead Letter Queue: messages that exhaust retries are moved to `{stream_name}_dlq`. This is well-implemented with consumer group pending message claiming. However, there is **no monitoring, no alerting, and no dashboard** for DLQ depth. Messages can accumulate in the DLQ indefinitely with no one knowing. |
| **Failure** | Critical extraction failures accumulate silently in DLQ → discovered days/weeks later → data gap for that period. |
| **Severity** | **P1** — silent data loss |
| **Confidence** | **HIGH** — DLQ exists (code verified) but no monitoring found |

### Finding 7.3 — Partial success: Google Maps extraction is all-or-nothing (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/extractors/strategies/google_maps.py:301` (single try/except for entire `_entry_to_data`), `src/extractors/strategies/google_maps_place.py:40-42` (returns [] on no data) |
| **Scenario** | `_entry_to_data` maps 17+ fields from a nested JSON array. The entire mapping is wrapped in a single `try/except Exception`. If field #14 (of 17) raises, fields 1-13 are lost — nothing is returned. There is no per-field isolation, no partial result, and no indication of *which* field failed. |
| **Failure** | Google adds/removes a field in their JSON → entire place result is discarded → zero records extracted even though 90% of fields were mappable. |
| **Severity** | **P2** — data completeness issue |
| **Confidence** | **HIGH** — verified in google_maps.py:301 |

### Finding 7.4 — No job heartbeat: scraper crash leaves RUNNING jobs forever (P1)

| Field | Value |
|-------|-------|
| **Location** | `src/domain/models.py:17-36` (JobState machine), `worker_scraper.py` (no heartbeat), `src/infrastructure/repositories/job_repository.py` (no stale job detection) |
| **Scenario** | Jobs transition `QUEUED → RUNNING → SUCCEEDED/FAILED`. Once a job enters RUNNING, only the worker can transition it. If the scraper worker crashes, the job stays RUNNING **forever**. There is no heartbeat mechanism, no timeout that auto-transitions stale RUNNING jobs to FAILED, and no reaper process. |
| **Failure** | Worker crash → job stuck in RUNNING → API returns "RUNNING" forever → client waits indefinitely → no alert → manual intervention required. |
| **Severity** | **P1** — operational risk under any worker failure |
| **Confidence** | **HIGH** — verified in job state machine and worker code |

### Finding 7.5 — Retry logic: good in stream consumer, missing in outbox relay (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/infrastructure/queues/stream_queue.py:90-140` (consumer retry + backoff + DLQ), `src/infrastructure/outbox_relay.py` (no retry on Valkey push failure), `src/infrastructure/ai/client.py:100-107` (circuit breaker) |
| **Scenario** | The stream consumer has well-implemented retry with exponential backoff, max retries, and DLQ. The AI client has a circuit breaker. But the **outbox relay** — which reads from SQLite outbox and pushes to Valkey — has no retry logic. If Valkey is temporarily unavailable, outbox events are marked as FAILED and never retried (or retried only on next relay poll, depending on `mark_failed` behavior). |
| **Failure** | Brief Valkey restart → all pending outbox events marked FAILED → jobs never dispatched to scraper → API returns successful job creation but nothing happens. |
| **Severity** | **P2** — transient dependency failure causes permanent event loss |
| **Confidence** | **MEDIUM** — checked outbox_relay.py but need to verify exact retry flow in mark_failed |

### Finding 7.6 — Resource exhaustion: no disk-full or DB-pool-exhaustion handling (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/infrastructure/artifact_store.py` (writes to disk, no disk-space check), `src/config_settings.py` (DB pool size configurable, no exhaustion handling), `tests/test_resilience_oom_dlq.py` (OOM tested, but not disk/DB) |
| **Scenario** | The artifact store writes scraped HTML to `artifacts/{sha256}`. No disk space check before writing. The database connection pool has a configurable size (`DB_POOL_SIZE`) but no logic for pool exhaustion — requests just queue or timeout. There are no alerts for "disk > 90%" or "connection pool > 80% utilized." |
| **Failure** | Disk full → artifact writes fail → scraping jobs fail → no alert until someone notices. DB pool exhausted → API requests timeout → 503 errors. |
| **Severity** | **P2** — happens under sustained load or long uptime |
| **Confidence** | **HIGH** — verified in artifact_store and config_settings |

### Part 7 Summary

| Scenario | Trigger | Detection | Behavior | Worst-Case Impact | Mitigation | Severity | Confidence |
|----------|---------|-----------|----------|-------------------|------------|----------|------------|
| Duplicate message processing | Consumer crash before ACK | None | Message re-processed | Duplicate extraction, wasted AI cost | Add consumer dedup check | P1 | HIGH |
| DLQ accumulation | Messages exhaust retries | None — no monitoring | Messages accumulate silently | Data loss discovered days later | Monitor DLQ depth, alert | P1 | HIGH |
| Stale RUNNING jobs | Worker crash | None — no heartbeat | Job stuck RUNNING forever | Client hangs indefinitely | Add heartbeat + timeout reaper | P1 | HIGH |
| Partial extraction failure | Google changes JSON format | Exception swallowed at debug | All fields lost for that entry | Data completeness gap | Per-field isolation | P2 | HIGH |
| Outbox relay failure | Valkey temporarily down | Outbox event marked FAILED | Events not dispatched | Jobs never scraped | Add retry to outbox relay | P2 | MEDIUM |
| Resource exhaustion | Disk full / DB pool exhausted | No monitoring/alerting | Writes fail / requests timeout | Job failures, 503 errors | Add resource alerts | P2 | HIGH |

---

## ΜΕΡΟΣ 8: CONCURRENCY & DISTRIBUTED STATE

**Depth Budget: 4 findings**

### Finding 8.1 — No distributed locks on critical path (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/infrastructure/queues/stream_queue.py` (consumer groups provide mutual exclusion per message), `src/saga_orchestrator.py` (saga pattern, no locks) |
| **Scenario** | Valkey Streams consumer groups provide natural mutual exclusion — each message is delivered to exactly one consumer in the group. This works for the main pipeline. However, scheduled tasks (strategy evaluation, profile updates) and the outbox relay have **no lock**. If multiple instances run (e.g., docker-compose `replicas: 2` on scraper), they could both execute the scheduled task. |
| **Failure** | Two strategy evaluators run concurrently → conflicting profile updates → non-deterministic strategy selection. Two outbox relays poll concurrently → same events pushed twice. |
| **Severity** | **P2** — requires multiple instances + scheduled task collision |
| **Confidence** | **HIGH** — verified: no locking mechanism found for scheduled operations |

### Finding 8.2 — `update_job_state` lacks optimistic concurrency control (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/domain/ports.py:22` (`update_job_state` — "Atomically transition"), `src/infrastructure/repositories/job_repository.py` (implementation) |
| **Scenario** | The port documents `update_job_state` as "Atomically transition." But the protocol has no version/etag parameter — the implementation likely does a `SELECT` + `UPDATE` with a state condition. If two workers both try to transition the same job (e.g., processor succeeding and a timeout reaper failing), one transition is silently lost. |
| **Failure** | Lost update: processor sets job to SUCCEEDED, reaper simultaneously sets it to FAILED → final state depends on race timing, not correctness. |
| **Severity** | **P2** — race condition on job state transitions |
| **Confidence** | **MEDIUM** — ports.py specifies "atomically" but need to verify implementation uses CAS/WHERE clause |

### Finding 8.3 — Configuration drift: docker-compose vs enterprise (P2)

| Field | Value |
|-------|-------|
| **Location** | `docker-compose.yml` (1,991 B — SQLite + Valkey), `docker-compose.enterprise.yml` (9,347 B — PostgreSQL + Kafka + Prometheus + Grafana), `.env` vs `.env.example` |
| **Scenario** | Two deployment profiles exist with significantly different architectures: standard uses SQLite + Valkey Streams; enterprise uses PostgreSQL + Kafka. These are configured via separate compose files, not a single config with profiles. The codebase has conditional paths (e.g., `postgres_tracker.py` separate from `sqlite_tracker.py`). Configuration drift between these two paths is likely — a fix applied to one path may not be applied to the other. |
| **Failure** | Bug fixed in SQLite path → not fixed in PostgreSQL path → production (enterprise) has the bug while dev (standard) doesn't → "works on my machine." |
| **Severity** | **P2** — maintainability risk with dual backend |
| **Confidence** | **HIGH** — verified in compose files and source tree |

### Finding 8.4 — Split-brain: no partition tolerance strategy (P2)

| Field | Value |
|-------|-------|
| **Location** | `docker-compose.yml`, `docker-compose.enterprise.yml`, `src/infrastructure/queues/stream_queue.py` — `[ΔΕΔΟΜΕΝΟ ΕΛΛΙΠΕΣ]` for deployment topology |
| **Scenario** | The system is designed as a distributed cluster (API + scraper + processor + reporter + Redis + DB). In a network partition: (1) workers can't reach Redis → halt. (2) API can't reach DB → errors. (3) If Redis is partitioned from some workers but not others → consumer group rebalancing → messages delivered to wrong consumer. There is no explicit CAP trade-off declared and no partition-handling strategy documented. |
| **Failure** | Network partition → consumer group rebalances → messages potentially delivered to multiple consumers → duplicate processing. |
| **Severity** | **P2** — requires network partition to manifest |
| **Confidence** | **MEDIUM** — inferred from distributed architecture; no runtime partition data |

### Part 8 Summary

| Finding | Location | Scenario | Severity | Confidence |
|---------|----------|----------|----------|------------|
| 8.1 No distributed locks | Scheduled tasks, outbox relay | Concurrent scheduled execution | P2 | HIGH |
| 8.2 No optimistic concurrency | `ports.py:22` update_job_state | Lost update on job state race | P2 | MEDIUM |
| 8.3 Config drift | docker-compose vs enterprise | Dual backend path divergence | P2 | HIGH |
| 8.4 No partition tolerance | Distributed cluster topology | Duplicate processing on partition | P2 | MEDIUM |

---

## ΜΕΡΟΣ 9: ΕΞΑΡΤΗΣΕΙΣ ΚΑΙ ΕΦΟΔΙΑΣΤΙΚΗ ΑΛΥΣΙΔΑ

**Depth Budget: 4 findings**

### Finding 9.1 — No dependency lock file (P1)

| Field | Value |
|-------|-------|
| **Location** | `requirements.txt` (2,652 B), `requirements-enterprise.txt` (3,046 B) |
| **Scenario** | The project uses bare `requirements.txt` files with unpinned or loosely pinned versions (e.g., `fastapi>=0.100.0`). There is no `requirements.lock`, no `poetry.lock`, no `Pipfile.lock`, and no `pip freeze` output committed. Builds are **non-reproducible** — installing `requirements.txt` on different days can produce different dependency trees. |
| **Failure** | A transitive dependency releases a breaking change → CI suddenly fails → production deployment breaks → no way to bisect which dependency changed without a lock file. |
| **Severity** | **P1** — breaks reproducibility and makes incident response harder |
| **Confidence** | **HIGH** — no lock file found anywhere in repo |

### Finding 9.2 — Left-pad risk: direct PyPI dependency, no mirror (P2)

| Field | Value |
|-------|-------|
| **Location** | `requirements.txt`, `Dockerfile` (no `--index-url` override, no local PyPI mirror) |
| **Scenario** | All dependencies are fetched directly from PyPI at build time. There is no local mirror, no vendor directory, no hash-checking (`--require-hashes`), and no offline fallback. If a package is yanked from PyPI (left-pad scenario) or PyPI is unavailable during a deployment, the build fails. |
| **Failure** | PyPI outage during critical production deploy → Docker build fails → cannot deploy fix. |
| **Severity** | **P2** — low probability, high impact |
| **Confidence** | **HIGH** — verified in Dockerfile and requirements files |

### Finding 9.3 — Vendor lock-in: OpenAI/Gemini AI providers (P3)

| Field | Value |
|-------|-------|
| **Location** | `src/infrastructure/ai/client.py` (OpenAI SDK), `src/infrastructure/providers/enrichment_provider.py` (Gemini SDK) |
| **Scenario** | The AI enrichment path supports two providers (OpenAI and Gemini), which is better than single-provider lock-in. However, the prompt engineering is embedded directly in the AI client code (lines 132-199) with provider-specific SDK calls. Switching to a new provider (e.g., Anthropic, local LLM) requires rewriting both the SDK integration and re-testing all prompts. There is no abstraction layer between "AI task" and "AI provider." |
| **Failure** | OpenAI price increase or deprecation → migration requires significant code changes. |
| **Severity** | **P3** — dual-provider support already mitigates worst case |
| **Confidence** | **HIGH** — verified in ai/client.py and enrichment_provider.py |

### Finding 9.4 — License compatibility: unverified (P3)

| Field | Value |
|-------|-------|
| **Location** | `requirements.txt` — 40+ packages with no license audit |
| **Scenario** | The project depends on 40+ packages across the core and enterprise requirements. No license check is performed. Copyleft licenses (GPL, AGPL) would be incompatible with a proprietary deployment. Packages with no license are legal risk. `[ΔΕΔΟΜΕΝΟ ΕΛΛΙΠΕΣ]` — need `pip-licenses` output to verify. |
| **Failure** | GPL-licensed transitive dependency → legal requirement to open-source proprietary code → compliance violation. |
| **Severity** | **P3** — legal/compliance risk |
| **Confidence** | **LOW** — no license data collected; pure assumption |

### Part 9 Summary

| Dependency | Version | License | CVE | Maintenance | Blast Radius | Severity | Confidence |
|-----------|---------|---------|-----|------------|-------------|----------|------------|
| All (40+ pkgs) | Unlocked | Unverified | Unknown | Unknown | Full build reproducibility | P1 | HIGH |
| PyPI (registry) | N/A | N/A | N/A | N/A | All builds during outage | P2 | HIGH |
| OpenAI/Gemini SDKs | Unpinned | Proprietary | N/A | Active | AI enrichment path | P3 | HIGH |
| Transitive deps | Unknown | Unverified | Unknown | Unknown | Legal exposure | P3 | LOW |

---

## ΜΕΡΟΣ 10: ΑΠΟΔΟΣΗ ΠΟΥ ΔΕΝ ΦΑΙΝΕΤΑΙ ΣΤΟ PROFILER

**Depth Budget: 4 findings**

### Finding 10.1 — Logging overhead: synchronous debug logs in hot path (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/extractors/strategies/google_maps.py:301` (`logger.debug(..., exc_info=True)`), `src/extractors/universal_strategy.py:73,182` (debug logging in extraction loop), `src/infrastructure/logger_config.py` (synchronous JSON logging) |
| **Scenario** | The extraction pipeline (hot path for every record) uses synchronous `logger.debug()` calls with `exc_info=True` (which captures full stack traces). In production with `LOG_LEVEL=DEBUG`, this adds significant I/O overhead per record. Even with higher log levels, the function call overhead of constructing debug messages (f-strings) is paid regardless of whether the log is emitted. |
| **Failure** | Production mistakenly configured with DEBUG level → extraction throughput drops significantly. |
| **Severity** | **P2** — configuration error induces performance penalty |
| **Confidence** | **HIGH** — verified in extraction code |

### Finding 10.2 — Orphaned resources: `_compute_embedding_cached` leaks `self` reference (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/infrastructure/ai/client.py:211-219` (`_compute_embedding_cached` with `@lru_cache`) |
| **Scenario** | `_compute_embedding_cached` is decorated with `@lru_cache(maxsize=256)`. Because it's an instance method, `self` is part of the cache key. Since `self` is a different object each time the method is called (or the same object with different internal state), the cache **never hits** — every call is a cache miss. This means: (1) embeddings are recomputed every time (performance bug), and (2) the LRU cache grows with entries that will never be evicted by hits, leaking memory. |
| **Failure** | Embedding cache is effectively disabled → redundant AI API calls → increased latency and cost. |
| **Severity** | **P2** — silent performance regression |
| **Confidence** | **HIGH** — verified in code; `@lru_cache` on instance methods is a well-known anti-pattern |

### Finding 10.3 — Connection management: no pool exhaustion monitoring (P2)

| Field | Value |
|-------|-------|
| **Location** | `src/config_settings.py` (DB_POOL_SIZE, REDIS_POOL_SIZE), `src/infrastructure/browser/pool.py` (browser context pool) |
| **Scenario** | The database, Redis, and browser all use connection pools with configurable sizes. However, there is no monitoring of pool utilization, no metrics for "pool exhausted" events, and no alerting when pools approach capacity. If the DB pool is exhausted, requests queue or timeout with no visibility into why. |
| **Failure** | Pool exhaustion → 503 errors → no metric to diagnose → "the site is down" with no root cause indicator. |
| **Severity** | **P2** — debuggability gap under load |
| **Confidence** | **HIGH** — verified: no pool metrics in observability setup |

### Finding 10.4 — `GenericStrategy` creates new `DeterministicExtractionPipeline` per call (P3)

| Field | Value |
|-------|-------|
| **Location** | `src/extractors/strategies/generic.py:26-28` |
| **Scenario** | Every call to `GenericStrategy.extract()` instantiates a new `DeterministicExtractionPipeline` object. The pipeline itself is stateless, but this pattern bypasses dependency injection and creates unnecessary object allocation per call. More importantly, it means any initialization logic in the pipeline runs on every extraction, not once. |
| **Failure** | Minor GC pressure under high throughput. Not a production risk at current scale. |
| **Severity** | **P3** — micro-optimization |
| **Confidence** | **HIGH** — verified in generic.py |

### Part 10 Summary

| Problem | Location | Mechanism | Impact | Severity | Confidence |
|---------|----------|-----------|--------|----------|------------|
| Sync debug logging in hot path | `google_maps.py:301`, `universal_strategy.py:73,182` | `exc_info=True` stack capture in extraction loop | Throughput drop with DEBUG level | P2 | HIGH |
| Broken LRU cache on embeddings | `ai/client.py:211-219` | `@lru_cache` on instance method → `self` in cache key | Cache never hits, redundant API calls | P2 | HIGH |
| No pool utilization metrics | `config_settings.py`, `browser/pool.py` | No monitoring of pool saturation | Cannot diagnose pool exhaustion | P2 | HIGH |
| Redundant pipeline instantiation | `generic.py:26-28` | New pipeline per extraction call | Minor GC overhead | P3 | HIGH |

---

## ΤΕΛΙΚΗ ΑΝΑΦΟΡΑ

### Executive Summary

**System:** Spacescraper — distributed web extraction service (FastAPI + Playwright + Valkey + AI enrichment)

**Top 5 Critical Findings (P0/P1):**

| # | Finding | Blast Radius |
|---|---------|-------------|
| 1 | **No ownership model (IDOR)** — any API key accesses all resources | All data in the system |
| 2 | **Naive datetimes everywhere** — `datetime.utcnow()` in 23 model fields | All timestamps, comparisons, DST logic |
| 3 | **`hash()` non-determinism** — schema IDs break on restart | Override extraction path |
| 4 | **Correlation IDs don't cross API→worker** — end-to-end tracing broken | All observability |
| 5 | **No job heartbeat** — worker crash leaves jobs RUNNING forever | Job lifecycle integrity |

**Single Point of Failure:** **Valkey/Redis** — all inter-service communication flows through it. No failover, no cluster mode in standard deployment.

**First 3AM Alert Prediction:** Worker crash → jobs stuck in RUNNING → on-call paged → no correlation IDs to trace → no runbooks to follow → manual Redis inspection required.

**One Change → Maximum Reliability:** Implement **job heartbeat + stale job reaper**. This prevents the most common silent failure mode (worker crash → zombie jobs) and gives immediate visibility into worker health.

### Severity Summary

| Part | P0 | P1 | P2 | P3 | Confidence Avg |
|------|----|----|----|-----|---------------|
| 1. Χρονική Συμβατότητα | 0 | 1 | 3 | 0 | HIGH |
| 2. Σχεδιαστικές Αποφάσεις | 0 | 2 | 4 | 0 | HIGH |
| 3. Παρατηρησιμότητα & Κόστος | 0 | 1 | 3 | 1 | HIGH |
| 4. Ανθρώπινοι Παράγοντες | 0 | 1 | 2 | 1 | HIGH |
| 5. Ασφάλεια | 0 | 4 | 3 | 0 | HIGH/MEDIUM |
| 6. Διαχείριση Δεδομένων | 0 | 0 | 3 | 2 | HIGH/MEDIUM |
| 7. Αντιμετώπιση Αστοχίας | 0 | 3 | 3 | 0 | HIGH/MEDIUM |
| 8. Concurrency & State | 0 | 0 | 4 | 0 | HIGH/MEDIUM |
| 9. Εξαρτήσεις | 0 | 1 | 1 | 2 | HIGH/MEDIUM/LOW |
| 10. Απόδοση | 0 | 0 | 3 | 1 | HIGH |
| **ΣΥΝΟΛΟ** | **0** | **13** | **29** | **7** | — |

### Ship Decision

**CONDITIONAL** — 13 P1 findings exist, but many have active partial mitigations:
- Idempotency keys exist (just not enforced at consumers)
- DLQ exists (just not monitored)
- SSRF protection exists (just has TOCTOU gap)
- Circuit breaker exists (just not for all paths)

No P0s — nothing is actively on fire. But P1s will cause production incidents under normal operation (crash recovery, multi-tenant deployment, DST transitions).

### Prioritized Action List

| Priority | Action | Owner | ETA | Blocks Deployment | Blast Radius |
|----------|--------|-------|-----|-------------------|-------------|
| 1 | Add job heartbeat + stale job reaper | Backend | 3d | No | Job lifecycle |
| 2 | Replace `datetime.utcnow()` with `datetime.now(tz=UTC)` | Backend | 2d | No | All timestamps |
| 3 | Propagate correlation IDs across workers | Backend | 2d | No | Observability |
| 4 | Add consumer-side message_id dedup | Backend | 1d | No | Duplicate processing |
| 5 | Replace `hash()` with `hashlib.sha256` in OverrideStrategy | Backend | 1d | No | Schema lookup |
| 6 | Add DLQ depth monitoring + alert | Platform | 1d | No | Silent data loss |
| 7 | Add PII redaction before AI API calls | Backend | 3d | No | GDPR compliance |
| 8 | Add non-root user to Dockerfile | Platform | 1d | No | Container security |
| 9 | Add dependency lock file | Platform | 1d | No | Build reproducibility |
| 10 | Write operational runbooks | Tech Lead | 5d | No | Incident response |

### Uncertainty Register

1. **Top 3 claims most likely to be wrong:**
   - *Claim:* Multi-tenancy is planned (Finding 5.2) — may be intentionally single-tenant, making IDOR a P3 not P1.
   - *Claim:* `update_job_state` lacks CAS (Finding 8.2) — the implementation may use a WHERE clause for atomicity despite the protocol not showing it.
   - *Claim:* Outbox relay has no retry (Finding 7.5) — need to verify the exact `mark_failed` → `get_pending_events` retry loop.

2. **Requires runtime validation:**
   - DNS rebinding TOCTOU (Finding 5.3) — static analysis shows the gap; runtime test with controlled DNS needed.
   - API key timing side-channel (Finding 5.4) — need timing measurements.
   - Consumer group rebalancing under partition (Finding 8.4) — need chaos engineering.

3. **Requires additional context:**
   - Multi-tenancy intent (Finding 5.2)
   - GDPR applicability — does the system scrape personal data?
   - License audit — `pip-licenses` output needed
   - CI/CD pipeline — not found in repo, may exist externally

4. **[ΕΙΚΑΣΙΑ] items needing confirmation:**
   - Cron overlap behavior (Finding 1.3) — need runtime observation
   - Outbox relay retry flow (Finding 7.5) — need full code path trace
   - `update_job_state` atomicity (Finding 8.2) — need implementation verification

---

## JSON SUMMARY

```json
{
  "audit_type": "beyond-the-obvious-v7",
  "system_name": "Spacescraper",
  "audit_date": "2025-07-17",
  "scope": "all",
  "audience": "Tech Lead",
  "input_available": ["source_code", "dependency_manifests", "config_files", "docker_files", "documentation", "tests"],
  "input_missing": ["architecture_diagrams", "ci_cd_config", "runtime_logs", "developer_interview"],
  "total_findings": {
    "P0": 0, "P1": 13, "P2": 29, "P3": 7
  },
  "by_category": {
    "temporal": {"P0": 0, "P1": 1, "P2": 3, "P3": 0, "confidence_avg": "HIGH"},
    "design_decisions": {"P0": 0, "P1": 2, "P2": 4, "P3": 0, "confidence_avg": "HIGH"},
    "observability_cost": {"P0": 0, "P1": 1, "P2": 3, "P3": 1, "confidence_avg": "HIGH"},
    "human_factors": {"P0": 0, "P1": 1, "P2": 2, "P3": 1, "confidence_avg": "HIGH"},
    "security": {"P0": 0, "P1": 4, "P2": 3, "P3": 0, "confidence_avg": "HIGH"},
    "data_management": {"P0": 0, "P1": 0, "P2": 3, "P3": 2, "confidence_avg": "HIGH/MEDIUM"},
    "failure_handling": {"P0": 0, "P1": 3, "P2": 3, "P3": 0, "confidence_avg": "HIGH/MEDIUM"},
    "concurrency_state": {"P0": 0, "P1": 0, "P2": 4, "P3": 0, "confidence_avg": "HIGH/MEDIUM"},
    "dependencies": {"P0": 0, "P1": 1, "P2": 1, "P3": 2, "confidence_avg": "MEDIUM"},
    "performance": {"P0": 0, "P1": 0, "P2": 3, "P3": 1, "confidence_avg": "HIGH"}
  },
  "ship_decision": "CONDITIONAL",
  "blocking_items": [],
  "top_risk": "No ownership model (IDOR) — if multi-tenant, any API key accesses all resources",
  "single_point_of_failure": "Valkey/Redis — all inter-service communication",
  "blast_radius_map": {
    "Valkey": ["worker_scraper", "worker_processor", "worker_reporter", "outbox_relay"],
    "worker_scraper": ["worker_processor (via raw_payloads_stream)", "Job state integrity (stale RUNNING)"],
    "SQLite/PostgreSQL": ["main.py (API)", "all workers (state persistence)"],
    "OpenAI_API": ["AI enrichment path", "strategy evaluation"],
    "Playwright_Chromium": ["all scraping capability"]
  },
  "first_3am_alert_prediction": "Worker crash → zombie RUNNING jobs → on-call paged → no correlation IDs, no runbooks",
  "one_change_max_reliability": "Job heartbeat + stale job reaper",
  "uncertainty_register": {
    "likely_wrong": [
      "Multi-tenancy is planned (may be intentionally single-tenant)",
      "update_job_state lacks CAS (implementation may use WHERE clause)",
      "Outbox relay has no retry (need to verify mark_failed → retry loop)"
    ],
    "requires_runtime_validation": [
      "DNS rebinding TOCTOU (controlled DNS test)",
      "API key timing side-channel (timing measurements)",
      "Consumer group rebalancing under partition (chaos engineering)"
    ],
    "guesses_needing_confirmation": [
      "Cron overlap behavior (runtime observation)",
      "Outbox relay retry flow (full code path trace)",
      "update_job_state atomicity (implementation verification)",
      "Multi-tenancy intent (product decision)",
      "License compatibility (pip-licenses audit)"
    ]
  }
}
```
