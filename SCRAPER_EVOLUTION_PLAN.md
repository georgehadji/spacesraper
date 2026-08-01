# Scraper Evolution Implementation Plan

**Status:** Draft

## 1. Objective and Boundaries

Evolve Spacescraper into a reliable, generic, self-improving web-extraction service. The target product accepts a URL and optional extraction schema, fetches the page safely, produces validated generic records, persists job results and evidence, and publishes CSV, JSON, or signed webhooks.

This plan deliberately does not reintroduce procurement monitoring, WooCommerce, win prediction, or dashboards. Existing remnants of those features are removed or generalized before new capabilities are added. The active architecture remains asynchronous and worker-based:

```text
FastAPI -> job store + Redis stream -> scraper -> processor -> outbox -> reporter
                                      |             |
                                   artifacts      evaluation/feedback
```

`ARCHITECTURE_REMEDIATION_PLAN.md` remains the source plan for the initial security and dependency-inversion work. The work below incorporates its mandatory items and extends it with the generic product direction.

## 2. Design Principles

- Use hexagonal architecture: domain and application code depend on typed ports; infrastructure supplies adapters at composition roots.
- Prefer immutable Pydantic value objects and explicit state transitions over ad-hoc attribute mutation.
- Use the Strategy pattern for fetch/extraction choices, Repository plus Unit of Work for persistence, and an Outbox for reliable event delivery.
- Treat AI as an optional, fallible adapter. It may propose candidates; it never mutates source code, schemas, or production policy directly.
- Keep the system at-least-once and idempotent. Every persisted job, record, artifact, and delivery has a stable ID and retry-safe write path.
- Make every automatic decision observable, reversible, feature-flagged, and tied to evidence.

## 3. Phase 0: Baseline, Scope Cleanup, and Security Gates

### Scope cleanup

1. Replace `Opportunity`, `Product`, `Lead`, and `Article` special cases in `src/domain/models.py` with a generic `ExtractedRecord` model: `record_id`, `record_type`, `schema_version`, `canonical_url`, `source_url`, `data`, `identity_hash`, `content_hash`, `first_seen`, `last_seen`, and `change_type`.
2. Replace `UniversalExtractionStrategy` with schema-driven extraction. Remove procurement and product heuristics, `ProcurementClassifier`, WooCommerce fields/enrichment, `sources.yaml` procurement configuration, `Opportunity` persistence, and product/procurement examples and tests.
3. Remove `ReportGenerator.generate_pulse_dashboard` and its invocation in `worker_reporter.py`. Retain only CSV/JSON/XLSX artifacts, renamed around generic records.
4. Update `pipeline_config.yaml`, Docker comments, README, `.env.example`, and class/module names so they describe generic extraction only. Delete obsolete documents and scripts rather than retaining stale claims.

### Mandatory security and correctness fixes

1. Implement persistent hashed API-key storage and remove the `ss_` prefix bypass in `src/auth_middleware.py`. Generated keys must be saved; unknown and revoked keys must fail.
2. Apply `validate_outbound_url` at API submission, recursive-link admission, turbo HTTP requests, and user webhooks. Enforce HTTPS webhooks in production.
3. Sanitize and size-limit `/autograph` HTML before provider calls. Redact secrets, cookies, authorization headers, and query parameters before artifacts or prompts are stored.
4. Replace wildcard CORS with configured origins. Never permit `allow_credentials=True` with a wildcard origin.
5. Await each `metrics_tracker.increment` call in `src/smart_crawler.py`; replace `time.sleep` with `await asyncio.sleep` in the AI adapter.
6. Pass `force_refresh` through the cache API and call `update_url_cache` after a successful fetch. Cover cache miss, 304, unchanged ETag, force refresh, and update behavior with tests.

**Exit gate:** the existing SSRF/auth/security tests pass; new tests prove URL validation at every ingress; no references to dashboard, WooCommerce, procurement, tender, or win prediction remain outside historical archived plans.

## 4. Target Modules and Contracts

### Domain (`src/domain/`)

Define pure models and protocols:

- `Job`, `JobAttempt`, `JobState` (`QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `DEAD_LETTERED`) as a guarded state machine.
- `RawPage`, `ExtractedRecord`, `ExtractionSchema`, `OverlayVersion`, `DomainProfile`, `StrategyObservation`, `FeedbackItem`, and `EvaluationResult`.
- Ports: `JobRepository`, `RecordRepository`, `ArtifactStore`, `DomainProfileRepository`, `OverlayRepository`, `FeedbackRepository`, `EventPublisher`, `FetchClient`, and `EnrichmentProvider`.

Keep schemas versioned and validated. An `ExtractionSchema` defines allowed record fields, identity fields, required fields, and quality rules; `data` is validated against that schema before persistence. Do not place HTTP, Redis, SQL, or provider logic here.

### Application (`src/application/`)

Use small use-case services, each constructor-injected with ports:

- `SubmitJob`: validates request policy, creates durable job state, and writes an outbox command in one transaction.
- `ProcessFetch`: obtains a fetch strategy from `FetchStrategySelector`, saves a redacted `RawPage` artifact, and records attempt telemetry.
- `ExtractRecords`: applies the active overlay first, then deterministic structured-data strategies (JSON-LD, microdata, XHR JSON); only then invokes the AI fallback.
- `ValidateAndPersistRecords`: validates schema, computes stable hashes, deduplicates, writes records, and produces a generic change event.
- `EvaluateCandidate` and `ApplyFeedback`: update evidence without changing production policy inline.
- `DeliverEvent`: serializes exports and webhooks from an outbox record and records idempotent delivery status.

Use a Pipeline/Chain-of-Responsibility only within `ExtractRecords`; each stage returns typed `ExtractionCandidate` values and diagnostics. A failed optional stage must not erase results from an earlier one.

### Infrastructure (`src/infrastructure/`)

- Replace direct singleton imports with adapter factories in a single composition module, for example `src/bootstrap.py`.
- Keep SQLite as the default adapter, but use a migration runner with numbered migrations. Introduce PostgreSQL through the same repository ports only after the SQLite path is complete.
- Store artifacts behind `ArtifactStore`: local filesystem first with a content-addressed layout (`artifacts/{sha256}`), then an S3-compatible adapter when needed.
- Replace Redis-list consumption with Redis Streams and consumer groups. Acknowledge only after successful handling; retry with capped attempts and route exhausted messages to a DLQ. Preserve idempotency keys in the database.
- Implement provider adapters (`NoOpEnrichmentProvider`, `GeminiEnrichmentProvider`) behind `EnrichmentProvider`. Provider/model choice, timeouts, concurrency, cost limits, and enablement belong in `AISettings`, not module globals.
- Add a two-level embedding cache: small local LRU plus Redis keyed by provider/model/content hash. Delete the misleading unused `compute_embedding` wrapper.

## 5. Job Lifecycle, Persistence, and API

Create generic tables/repositories for `jobs`, `job_attempts`, `raw_artifacts`, `records`, `record_versions`, `domain_profiles`, `overlay_versions`, `strategy_observations`, `feedback`, `evaluation_runs`, `outbox_events`, and `deliveries`.

Migrate safely: export and back up the current SQLite file, add generic tables alongside legacy tables, backfill only data that has a defined generic schema, run read-only verification, then remove legacy tables and code in a later release. Do not silently reinterpret old procurement data.

Add API contracts:

- `POST /jobs` returns `202` with job ID and status URL.
- `GET /jobs/{job_id}` returns state, attempt summaries, record count, artifact references, and sanitized error details.
- `POST /jobs/{job_id}/cancel` is idempotent and prevents new attempts/fan-out.
- `GET /jobs/{job_id}/records` supports cursor pagination and JSON/CSV negotiation.
- `POST /overlays/validate` evaluates a proposed overlay against supplied or stored fixtures; promotion is a separate operation.
- `POST /records/{record_id}/feedback` accepts `accepted`, `rejected`, or a corrected record with a reason.

Use Pydantic request/response models at the HTTP boundary. Propagate `correlation_id`, `root_job_id`, overlay version, and schema version through every queue message as declared model fields. Remove runtime attribute injection. Respect each job's `max_depth`; do not use a hard-coded depth limit.

## 6. Fetching and Extraction Enhancements

1. Make `FetchStrategy` a protocol with `DirectHttpFetch` and `PlaywrightFetch` adapters. Both must apply the same outbound URL policy, per-domain rate limiter, timeout, and correlation metadata.
2. Add a `DomainProfile` policy object containing robots/allow-list decision, rate budget, browser-required state, active overlay, preferred strategy, last page fingerprint, and profile version.
3. Use deterministic extraction in order: validated overlay, XHR/JSON, JSON-LD, semantic HTML patterns, then AI candidate. This lowers cost and makes results reproducible.
4. Version overlays with `CANDIDATE`, `SHADOW`, `ACTIVE`, `DISABLED`, and `RETIRED` states. Store selector mappings, schema version, validation suite result, author, source evidence, and rollback target.
5. Persist page fingerprints and response validators. Cache only sanitized metadata and content hashes; serve a cache hit only when validator/fingerprint rules confirm equivalence.
6. Add per-domain concurrency budgets and an adaptive browser-pool size read from `ScraperSettings.pool_size`. Do not allow a burst from recursive discovery to exceed job and domain budgets.

## 7. Self-Learning and Self-Optimization

The learning system optimizes extraction policy, not application code. It operates asynchronously outside the request path.

### Evidence collection

For every attempt, append an immutable `StrategyObservation`: domain/profile version, strategy, overlay version, input fingerprint, valid-record count, required-field completeness, duplicate rate, HTTP/block outcome, latency, browser seconds, AI tokens/cost, and operator feedback. Store redacted fixtures separately with retention rules.

### Candidate generation and evaluation

1. When deterministic extraction fails quality gates, the AI provider may propose an overlay candidate.
2. Run candidates in shadow mode against retained fixtures and a held-out sample. Compare them with the active overlay using precision, completeness, valid-record rate, cost, and latency.
3. Require configurable minimum evidence (for example, 20 successful validations across multiple fingerprints), no schema violations, and non-inferior precision before a candidate can enter canary mode.
4. Promote through `CANDIDATE -> SHADOW -> CANARY -> ACTIVE`; retain the previous active version for immediate rollback. The promotion service writes an audit event and requires an explicit service policy; a human approval flag should be the default initially.

### Strategy selection

Start with a deterministic utility score. After sufficient data, choose HTTP versus browser using a bounded exploration policy such as Thompson sampling on a success-quality threshold, with cost and block-rate guardrails. Limit exploration to a configurable small percentage, never explore on blocked/unsafe domains, and immediately demote a strategy after a sustained quality or error regression. Shared counters and profile state live in Redis/database, never in one worker's memory.

### Feedback and evaluation

Feedback is labeled training data, not an immediate instruction. Corrections create a new record version and a `FeedbackItem`; a scheduled evaluator aggregates results by domain, strategy, schema, and overlay. Run a nightly evaluation on a fixed regression corpus and publish a versioned report. Fail deployment when core metrics regress beyond configured tolerances.

## 8. Reliable Reporting and Webhooks

Replace dashboard generation with an `ArtifactWriter` and `DeliveryAdapter` ports. Implement `CsvArtifactWriter`, `JsonArtifactWriter`, optional XLSX writer, `SignedWebhookDelivery`, and `SlackDelivery` as adapters. Queue delivery through `outbox_events`; use a deterministic event ID and HMAC signature header for customer webhooks. Validate outbound delivery URLs, use bounded retries with backoff, and record delivery state for support/replay.

## 9. Observability and Operations

Emit structured logs and OpenTelemetry spans for API submission, queue wait, fetch, extraction stage, validation, persistence, event delivery, and learning decisions. Required metrics: job-state totals, queue age, retry/DLQ count, cache accuracy, fetch strategy success, valid-record yield, schema completeness, dedup rate, AI degradation/cost, block rate, p50/p95 latency, overlay promotion/rollback count, and delivery failures.

Provide Prometheus metrics and alert rules rather than a project dashboard. Alerts should target sustained queue age, rising block rate, DLQ growth, broken active overlays, delivery failure, and cost-budget breach. Use correlation ID to trace one job end-to-end.

## 10. Test and Delivery Strategy

Use pytest with isolated fake adapters for unit tests, fakeredis for stream/profile tests, temporary SQLite for repository tests, and Playwright only for a small labelled integration suite. Add contract tests for every port adapter, golden-fixture tests for overlays, property tests for record hashing/idempotency, and end-to-end tests for API -> stream -> workers -> records -> outbox.

Run quality gates in CI: formatting/linting/type checking, unit tests, integration tests, migration-up/migration-down smoke test, dependency/security scan, and a regression evaluation suite. Measure coverage on `src/`; enforce at least 80% on new or materially changed application/domain code.

Deploy behind feature flags in this order: generic schema and repositories, durable job lifecycle, streams/outbox, deterministic extraction, adaptive profiles, shadow overlays, then canary learning. Each release needs migration backup verification, metrics comparison, a documented rollback path, and no unresolved DLQ growth.

## 11. Sequenced Delivery Increments

1. **Foundation:** Phase 0 cleanup/security/correctness, settings consolidation, correlation IDs, and baseline tests.
2. **Reliable core:** generic models, repositories/migrations, durable jobs, typed messages, status/result API, streams, and outbox.
3. **Generic extraction:** schema/overlay contracts, deterministic extraction pipeline, generic persistence/exports, artifact storage, and cache repair.
4. **Efficiency:** per-domain budgets, configured browser pools, provider adapters, bounded AI concurrency, and shared caches.
5. **Learning:** observations, feedback, offline evaluator, profile registry, shadow overlays, and human-gated promotion.
6. **Autonomy:** canary promotion, bounded strategy exploration, automated rollback, SLO alerts, and production load/failure drills.

No increment advances until its tests, migration checks, security checks, and observability acceptance criteria pass. This keeps self-optimization a controlled capability built on a stable generic scraper rather than another source of scope creep.
