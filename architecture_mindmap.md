# ARCHITECTURE MINDMAP — Spacescraper (Forensic Reconstruction)

## 1. SYSTEM IDENTITY
- **Primary Language:** Python 3.12.10
- **Frameworks:** FastAPI 0.139.2, Pydantic 2.13.4, httpx 0.28.1, BeautifulSoup4 4.13.3, aiosqlite 0.22.1, valkey-py 6.1.1
- **Architectural Style:** Hexagonal (ports-and-adapters) for data/persistence layer combined with async event-driven pipeline for extraction — detected via `src/domain/ports.py` containing 5 Protocol classes imported by `src/application/` modules and implemented by `src/infrastructure/repositories/`
- **Entry Points:**
  - `main.py:243` — `uvicorn.run("main:app", host="0.0.0.0", port=8000)` (FastAPI)
  - `worker_scraper.py:290` — `asyncio.run(worker.run())`
  - `worker_processor.py:135` — `asyncio.run(worker.run())`
  - `worker_reporter.py:90` — `asyncio.run(worker.run())`
  - `boot.py:56` — `asyncio.run(main())` (orchestrates all 4 subprocesses)
- **Build/Config Files:** `requirements.txt`, `requirements-enterprise.txt`, `Dockerfile`, `docker-compose.yml`, `docker-compose.enterprise.yml`, `pytest.ini`, `.env.example`

---

## 2. MODULE INVENTORY

### Domain `src/domain/`
- **Responsibility:** Pure Pydantic data models and Protocol ports — zero infrastructure dependencies
- **Type:** core logic
- **Exports:** 15+ Pydantic `BaseModel` classes (Job, JobAttempt, JobState, ExtractedRecord, QueueMessage, ExtractionSchema, ExtractionOverlay, OutboxEvent, StrategyObservation, FeedbackItem, EvaluationResult, DomainProfile, + legacy Opportunity/Product/Lead/Article), 6 Protocol classes (JobRepository, RecordRepository, OutboxRepository, OverlayRepository, ObservationRepository, ArtifactStore), 4 enums (JobState, ChangeType, MessageType, OverlayState), 2 exceptions (SpacescraperError subclasses)
- **Internal Structure:**
  - `models.py` — All domain entities (185 lines, 15+ models, 4 enums)
  - `ports.py` — Repository protocol interfaces (8 methods JobRepository, 4 RecordRepository, 5 OutboxRepository, 8 OverlayRepository, 6 ObservationRepository)
  - `exceptions.py` — Domain exception hierarchy (SSRFGuardError, InputValidationError, ScrapeFailure, etc.)
- **Dependencies:**
  - → External: pydantic@2.13.4 — all model definitions
  - → External: enum — state machine and type enums
  - No internal dependencies (pure domain)

### Application `src/application/`
- **Responsibility:** Use-case services implementing business logic using domain models and repository ports
- **Type:** core logic
- **Exports:** DeterministicExtractionPipeline, StrategyEvaluator, ShadowOverlayEvaluator, StrategySelector, ExplorationPolicy, DataPipeline (legacy), IntelligencePostProcessor (legacy)
- **Internal Structure:**
  - `extraction_pipeline.py` — Deterministic strategy chain: overlay → JSON-LD → semantic HTML with schema validation. Depends on `OverlayRepository` (protocol) via DI
  - `evaluator.py` — StrategyEvaluator computes composite utility scores from observations, produces EvaluationResults. Depends on `ObservationRepository` (protocol) via DI
  - `shadow_evaluator.py` — ShadowOverlayEvaluator runs CANDIDATE overlays against ACTIVE, produces scores. Depends on `OverlayRepository` (protocol) via DI
  - `strategy_selector.py` — Background loop selecting best strategy per domain. Depends on `ObservationRepository` (protocol) via DI
  - `exploration_policy.py` — Thompson sampling exploration with blocked-domain exclusion. Zero infrastructure deps
  - `pipeline.py` — Legacy ETL pipeline (Opportunity extraction, dedup, AI enrichment — neutered). Imports `ai_orchestrator` removed per remediation
  - `post_processor.py` — Legacy state auditor for Opportunity lifecycle. Uses DI for `SqliteTracker`
  - `classifier.py` — Defunct stub (ProcurementClassifier removed in Phase 0)
  - `llm_enrichment.py` — Unused legacy OpenAI enrichment
- **Dependencies:**
  - → `src/domain/models.py` — all entity types
  - → `src/domain/ports.py` — OverlayRepository, ObservationRepository (protocols)
  - → `src/infrastructure/repositories/overlay_repository.py` — SqliteOverlayRepository (removed per remediation — now uses protocol)
  - → External: BeautifulSoup4 — HTML parsing in extraction_pipeline

### Infrastructure `src/infrastructure/`
- **Responsibility:** Concrete adapter implementations for all domain ports
- **Type:** infrastructure
- **Exports:** 5 SQLite repositories, Valkey stream queue, artifact store, rate limiter, AI cache, SLO monitor, enrichment providers, HTTP client, logging config
- **Internal Structure (sub-modules):**
  - `repositories/job_repository.py` — Implements `JobRepository` (jobs + job_attempts tables, WAL mode)
  - `repositories/record_repository.py` — Implements `RecordRepository` (records table, cursor pagination by record_id)
  - `repositories/outbox_repository.py` — Implements `OutboxRepository` (outbox_events table, retry tracking)
  - `repositories/overlay_repository.py` — Implements `OverlayRepository` (schemas + overlays tables)
  - `repositories/observation_repository.py` — Implements `ObservationRepository` (4 tables: observations, feedback, evaluations, profiles)
  - `queues/stream_queue.py` — Valkey Streams adapter (XADD/XREADGROUP/consumer groups/DLQ)
  - `queues/redis_worker.py` — Legacy LIST-based queue (BLPOP/RPUSH — being phased out)
  - `artifact_store.py` — Content-addressed file storage (artifacts/{xx}/{yy}/{sha256})
  - `rate_limiter.py` — Per-domain asyncio.Semaphore + Valkey SortedSet limiter
  - `cache.py` — Two-level LRU+Valkey AI cache
  - `slo_monitor.py` — 6 SLOs with warning/critical thresholds + AutoRollback
  - `outbox_relay.py` — Background service draining outbox_events to streams
  - `providers/enrichment_provider.py` — EnrichmentProvider ABC + NoOp + Gemini implementations
  - `http_client.py` — Singleton httpx.AsyncClient wrapper
  - `logger_config.py` — Production logging setup
  - `browser/engine.py` — Playwright browser automation
  - `browser/pool.py` — Browser context pool
  - `monitoring/observability.py` — Redis-backed metrics tracker
  - `notifications/notifier.py` — Slack/Discord alerting
  - `middleware/correlation.py` — Correlation ID propagation
  - `exports/artifact_writers.py` — CSV/JSON file writers for ExtractedRecords
  - `exports/report_generator.py` — Legacy Excel/CSV report generator (Opportunity-based)
  - `exports/plugins.py` — Slack/Webhook export plugins
  - `storage/sqlite_tracker.py` — Legacy Opportunity persistence
- **Dependencies:**
  - → `src/domain/models.py` — uses all domain entities
  - → External: aiosqlite@0.22.1 — all repositories
  - → External: valkey@6.1.1 — stream queue, rate limiter, cache
  - → External: httpx@0.28.1 — HTTP client
  - → External: BeautifulSoup4@4.13.3 — extraction pipeline

### Security `src/security/`
- **Responsibility:** Input validation, SSRF protection, CORS configuration
- **Type:** cross-cutting concern
- **Exports:** `validate_outbound_url()`, `sanitize_for_prompt()`, `sanitize_for_log()`, `validate_payload_size()`, `build_cors_origins()`
- **Internal Structure:**
  - `ssrf_guard.py` — DNS-resolving IP blocklist (RFC1918, loopback, link-local, AWS metadata)
  - `input_sanitizer.py` — API key/email redaction, prompt injection filtering, size limiting
  - `cors_config.py` — Environment-based CORS origin builder (never wildcard)
  - `__init__.py` — Empty
- **Dependencies:**
  - → `src/domain/exceptions.py` — SSRFGuardError, InputValidationError
  - → External: socket, ipaddress, urllib.parse, re (stdlib)

### Extractors `src/extractors/`
- **Responsibility:** HTML parsing strategies for web content extraction
- **Type:** interface (strategy pattern)
- **Exports:** BaseExtractionStrategy (ABC), UniversalExtractionStrategy (generic extraction)
- **Internal Structure:**
  - `base_extractor.py` — Abstract `extract()` method contract
  - `universal_strategy.py` — Schema-driven extraction producing ExtractedRecords (JSON-LD, semantic HTML, overlay)
- **Dependencies:**
  - → `src/domain/models.py` — ExtractedRecord, BaseEntity
  - → External: BeautifulSoup4 — HTML parsing

### Root (Composition) `[.py files in /]`
- `main.py` — FastAPI application (9 endpoints, CORS/SSRF/auth middleware, lifespan-managed repos)
- `bootstrap.py` — Single composition root (11 adapter/service instances)
- `worker_scraper.py` — Scraper worker (Valkey Streams consumer, job state machine, rate limiter)
- `worker_processor.py` — Processor worker (pipeline, record persistence, record count update)
- `worker_reporter.py` — Reporter worker (Valkey Streams consumer, artifact writers, Slack)
- `boot.py` — Multi-process launcher (API + 3 workers via subprocess)
- `src/auth_middleware.py` — API key management, JWT, rate limiting
- `src/config_settings.py` — Pydantic Settings (Database, Redis, Kafka, AI, Scraper configs)
- `src/smart_crawler.py` — HTTP cache validation (ETag, Last-Modified)

---

## 3. DEPENDENCY GRAPH (Mermaid)

```mermaid
graph LR
  B["bootstrap.py"] --> J["SqliteJobRepository"]
  B --> R["SqliteRecordRepository"]
  B --> O["SqliteOutboxRepository"]
  B --> OV["SqliteOverlayRepository"]
  B --> OB["SqliteObservationRepository"]
  B --> SQ["RedisStreamQueue"]
  B --> AS["LocalArtifactStore"]
  B --> RL["DomainRateLimiter"]
  B --> EV["StrategyEvaluator"]
  B --> SS["StrategySelector"]

  M["main.py"] --> B
  M --> AUTH["src/auth_middleware.py"]
  M --> AI["src/infrastructure/ai/client.py"]

  WS["worker_scraper.py"] --> SQ
  WS --> J
  WS --> OB
  WS --> AS
  WS --> RL
  WS --> SM["src/smart_crawler.py"]

  WP["worker_processor.py"] --> SQ
  WP --> J
  WP --> R
  WP --> APP["src/application/pipeline.py"]
  WP --> PP["src/application/post_processor.py"]
  WP --> US["src/extractors/universal_strategy.py"]

  WR["worker_reporter.py"] --> SQ
  WR --> AW["src/infrastructure/exports/artifact_writers.py"]
  WR --> RG["src/infrastructure/exports/report_generator.py"]

  APP --> DM["src/domain/models.py"]
  PP --> ST["src/infrastructure/storage/sqlite_tracker.py"]

  EV --> DM
  EV --> DP["src/domain/ports.py"]
  OV --> DP
  SQ --> "External: valkey@6.1.1"
  J --> "External: aiosqlite@0.22.1"
  AI --> "External: httpx@0.28.1"
```

---

## 4. DATA FLOW — TOP 3 CRITICAL PATHS

### Path 1: Job Submission → Scrape → Result
- **Sequence:** `POST /jobs (main.py:submit_job)` → `validate_outbound_url (src/security/ssrf_guard.py)` → `Job(job_id) (src/domain/models.py:Job)` → `job_repo.create_job(job) (job_repository.py:79)` → `OutboxRelay.create_outbox_event (outbox_relay.py)` → `redis_queue.push_job (redis_worker.py:57)` → Valkey Streams → `worker_scraper.py:process_job` → `rate_limiter.wait_for_slot (rate_limiter.py:82)` → `_update_job_state(RUNNING)` → `_create_attempt` → `ScraperEngine.start + crawl (browser/engine.py)` → `push_raw_payload` → `_update_job_state(SUCCEEDED)` → `_complete_attempt` → `artifact_store.store (artifact_store.py:85)` → `obs_repo.create_observation` → `worker_processor.py:process_payload` → `pipeline.process` → `post_processor.run_state_audit` → `record_repo.create_record` → `queue.push_event` → `worker_reporter.py:handle_event` → `write_artifacts (artifact_writers.py:90)`
- **State Changes:** Job: QUEUED→RUNNING→SUCCEEDED (job_repository.py:114). JobAttempt created (job_repository.py:149). Raw HTML stored as content-addressed artifact. ExtractedRecord persisted (record_repository.py:69). DiscoveryEvent published
- **Failure Modes:**
  - SSRF guard blocks URL → 400 response (main.py:200)
  - Valkey push fails → 500 (main.py:258)
  - Scraper engine fails → StealthViolation/ScrapeFailure → DLQ push (worker_scraper.py:230/238), Job→FAILED
  - Attempt creation fails → warning logged, flow continues (worker_scraper.py:70)
  - Record persistence fails → exception caught in `process_payload`, flow continues (worker_processor.py:56)
- **Observability Gap:** `_complete_attempt` on turbo empty-yield was previously missing (fixed at worker_scraper.py:141). Rate limiter `acquire` failure logs warning but does not increment a metric (worker_scraper.py:112). `update_job_state` errors silently caught with warning only (worker_scraper.py:58)

### Path 2: Overlay Promotion Lifecycle
- **Sequence:** `POST /overlays/{id}/promote (main.py)` → `SqliteOverlayRepository.get_overlay` → validate target state + human approval gate → `update_overlay_state (overlay_repository.py:162)` → ACTIVE promotion retires old active → returns promoted status
- **State Changes:** Overlay state transition (e.g., CANDIDATE→SHADOW, SHADOW→ACTIVE). Old ACTIVE overlay → RETIRED. Updated DB row with new state + timestamp
- **Failure Modes:** Overlay not found → 404. Invalid transition path → 400. Human approval missing for ACTIVE → 400. DB update failure → exception propagates uncaught
- **Observability Gap:** No audit event is emitted when an overlay is promoted. Only the HTTP response conveys success. No outbox event for downstream consumers of promotion events

### Path 3: Background Strategy Evaluation
- **Sequence:** `StrategySelector.run_forever (strategy_selector.py:70)` → `asyncio.sleep(3600)` → `evaluate_all_domains` → `ObservationRepository.get_observations` filter by domain → `StrategyEvaluator.update_domain_profile` → `_compute_strategy_metrics` → `_compute_score` → `ObservationRepository.get_or_create_profile` → `update_profile`
- **State Changes:** DomainProfile updated: preferred_strategy, success_rate, total_observations. EvaluationResult persisted if `evaluate_strategy` is called separately
- **Failure Modes:** Observation query returns empty → loop continues silently. `update_profile` fails → warning only (strategy_selector.py:88). `get_or_create_profile` creates a new empty profile if none exists — always succeeds
- **Observability Gap:** Domain evaluation only logs on success (strategy_selector.py:80). Silent failure on observation query error. No metric for "evaluation completed" — only log messages

---

## 5. DESIGN PATTERNS & DECISIONS

| Pattern | Evidence | Confidence | Rationale |
|---------|----------|------------|-----------|
| Hexagonal (Ports & Adapters) | `src/domain/ports.py` has 5 Protocol classes; `src/infrastructure/repositories/` has 5 concrete SQLite implementations; `src/application/evaluator.py:33` imports `ObservationRepository` from ports not infrastructure | CONFIRMED | Dependency inversion — domain defines contracts, infra implements them |
| Strategy Pattern | `src/extractors/base_extractor.py` defines ABC `extract()`; `src/extractors/universal_strategy.py` overrides it; `DeterministicExtractionPipeline` extends with strategy chain | CONFIRMED | Pluggable extraction strategies per domain/page type |
| Repository Pattern | All 5 repositories in `src/infrastructure/repositories/` expose `get`, `create`, `update`, `list` methods behind Protocol interfaces | CONFIRMED | Clean data access abstraction per hexagonal architecture |
| Outbox Pattern | `src/infrastructure/outbox_repository.py` persists events atomically; `src/infrastructure/outbox_relay.py` drains them to streams asynchronously; `main.py:270` creates `job.submitted` event after job creation | CONFIRMED | Reliable event delivery — events survive before stream transmission |
| State Machine | `src/domain/models.py:14-33` defines `JobState` enum with `can_transition_to()` guard; `Job.transition_to()` enforces state transitions via `model_copy()` | CONFIRMED | Immutable state transitions with explicit allowed paths |
| Chain of Responsibility | `src/application/extraction_pipeline.py:41-67` chains 3 strategies: overlay→JSON-LD→semantic HTML; each stage returns validated records or delegates to next | CONFIRMED | Deterministic extraction with progressive fallback |
| Strategy (auto-selection) | `src/application/strategy_selector.py` evaluates domains periodically; `ExplorationPolicy` uses Thompson sampling; `StrategyEvaluator` computes composite utility scores | CONFIRMED | Auto-optimization of extraction strategies per domain |
| Circuit Breaker | `src/infrastructure/ai/client.py:41-55` — `_check_circuit()` with `failure_count`/`breaker_threshold`/`offline_until`; opens after 5 failures, cools for 300s | CONFIRMED | Prevents cascading AI API failures |
| Singleton | `src/infrastructure/http_client.py:65` — `http_client = HttpClient()`; `src/infrastructure/monitoring/observability.py:153` — `metrics_tracker = ObservabilityMetrics()`. Usage: `api_key_manager`, `ai_orchestrator`, `intel_tracker` | CONFIRMED | Convenient for single-process — precludes clean DI in tests |

---

## 6. ENTITY MAP

| Entity | Key Fields | Defined In | Consumed By | Persistence |
|--------|------------|------------|-------------|-------------|
| Job | job_id:str, url:str, state:JobState, record_count:int, error_message:str?, created_at:datetime | `src/domain/models.py:35` | main.py, worker_scraper, worker_processor, job_repository | SQLite: jobs table |
| JobAttempt | attempt_id:str, job_id:str, state:JobState, started_at:datetime, finished_at:datetime?, worker_id:str? | `src/domain/models.py:65` | worker_scraper, job_repository | SQLite: job_attempts table |
| ExtractedRecord | record_id:str, record_type:str, data:dict, source_url:str, change_type:ChangeType, content_hash:str? | `src/domain/models.py:179` | extraction_pipeline, record_repository, artifact_writers | SQLite: records table |
| QueueMessage | message_id:str, message_type:MessageType, correlation_id:str?, payload:dict, retry_count:int | `src/domain/models.py:88` | stream_queue, all workers | In-memory (Valkey Streams) |
| ExtractionSchema | schema_id:str, fields:List[FieldDefinition], quality_rules:dict, validate_record() | `src/domain/models.py:114` | extraction_pipeline, overlay_repository | SQLite: extraction_schemas table |
| ExtractionOverlay | overlay_id:str, domain:str, state:OverlayState, container_selector:str?, field_mappings:dict | `src/domain/models.py:140` | extraction_pipeline, shadow_evaluator, overlay_repository | SQLite: extraction_overlays table |
| StrategyObservation | observation_id:str, job_id:str, domain:str, strategy:str, valid_record_count:int, latency_ms:float, success:bool | `src/domain/models.py:220` | evaluator, strategy_selector, observation_repository | SQLite: strategy_observations table |
| EvaluationResult | evaluation_id:str, domain:str, score:float, recommendation:str?, sample_size:int | `src/domain/models.py:238` | evaluator, observation_repository | SQLite: evaluation_results table |
| DomainProfile | domain:str, preferred_strategy:str, success_rate:float, total_observations:int | `src/domain/models.py:258` | strategy_selector, observation_repository | SQLite: domain_profiles table |
| OutboxEvent | event_id:str, aggregate_type:str, event_type:str, status:OutboxStatus, retry_count:int | `src/domain/models.py:108` | main.py, outbox_relay, outbox_repository | SQLite: outbox_events table |

---

## 7. RISK REGISTER

| Risk | Severity | Location | Evidence |
|------|----------|----------|----------|
| Single-connection SQLite shared by 5 repositories under concurrent write load | MEDIUM | `src/infrastructure/repositories/job_repository.py:57` — `_conn: Optional[aiosqlite.Connection]` (single connection) | All 5 `Sqlite*Repository` classes use a single `aiosqlite.Connection` with WAL mode. Concurrent writes from API + workers may contend. Plan calls for PostgreSQL adapter |
| Application-layer infrastructure leakage (post_processor) | LOW | `src/application/post_processor.py:9` — `from src.infrastructure.storage.sqlite_tracker import SqliteTracker` | Only remaining infrastructure import in application layer — used as type hint for DI. Legacy code being phased out |
| Valkey Streams consumer group not re-created on stream deletion | LOW | `src/infrastructure/queues/stream_queue.py:145` — `_ensure_group` called once before loop; NOGROUP error not handled inside loop | If stream is deleted and recreated, consumer hangs on NOGROUP until restart |
| Outbox `mark_failed` not transactional — TOCTOU on retry_count | LOW | `src/infrastructure/repositories/outbox_repository.py:106-123` — SELECT + UPDATE without transaction | With multiple relay processes, retry_count may be double-written. Single-relay deployment is safe |
| `update_job_state` in repository bypasses state machine guard | LOW | `src/infrastructure/repositories/job_repository.py:114` — raw SQL UPDATE without `can_transition_to()` check | All current callers validate before calling. Future callers might not |
| Global singleton `ai_orchestrator` blocks parallel testing | LOW | `src/infrastructure/ai/client.py:237` — `ai_orchestrator = AIOrchestrator()` | Module-level instantiation prevents mock injection in tests |

---

## 8. UNCERTAINTY LOG

| Question | Location | Possible Interpretations | Impact if Wrong |
|----------|----------|--------------------------|-----------------|
| Is `src/database_models.py` actively used? | `/src/database_models.py` | Orphaned SQLAlchemy models from pre-hexagonal era OR still used by migration scripts | May contain dead code; safe to remove if no imports reference it |
| Is `src/postgres_tracker.py` wired? | `/src/postgres_tracker.py` | Planned PostgreSQL adapter OR unused | If unused, it is dead code; if planned, it should match the repository port interfaces |
| Is `src/saga_orchestrator.py` wired? | `/src/saga_orchestrator.py` | Unwired saga pattern implementation OR ready-to-wire but deferred | May be dead code — `_persist_state()` is a TODO stub (line 256) |
| Are `src/observability_tracing.py` and `src/event_bus.py` initialized? | `/src/observability_tracing.py`, `/src/event_bus.py` | Designed but not initialized in `main.py:lifespan` | OpenTelemetry spans may not be emitted; event bus may not be publishing |
| Truncation: `src/infrastructure/browser/` (4 files) not analyzed past entry point — Playwright engine is runtime-dependent | `browser/engine.py`, `pool.py`, `persona.py`, `stealth_brain.py` | Browser context management | Browser-specific defects (memory leaks, dead sessions) are invisible to static analysis |
