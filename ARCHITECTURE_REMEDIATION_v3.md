# Spacescraper — Architecture Remediation Plan
## Based on V7 Deep Audit (2025-07-17) | 49 Findings → 7 Phases

---

## Overview

| Metric | Value |
|--------|-------|
| Total findings | 49 (0 P0, 13 P1, 29 P2, 7 P3) |
| Phases | 7 |
| Estimated total effort | ~25–30 engineering-days |
| Architecture style | Ports & Adapters (Hexagonal) — `domain → application → infrastructure` |
| Key constraint | Domain layer changes FIRST; adapters FOLLOW; entry points LAST |

### Dependency Graph (which phases block which)

```
Phase 1 (Domain Foundation)
  └─▶ Phase 2 (Observability Backbone)
        └─▶ Phase 3 (Resilience Core)
              ├─▶ Phase 4 (Security Hardening)
              ├─▶ Phase 5 (Data Lifecycle)
              └─▶ Phase 6 (Operational Excellence)
                    └─▶ Phase 7 (Architecture Cleanup)
```

---

## Phase 1: Domain Foundation (4 days)

**Why first:** Every other phase depends on correct domain models. Naive datetimes are actively producing inconsistent DB state (mixed aware/naive ISO strings). The `hash()` bug silently corrupts schema IDs on every restart. No other fix is safe until these are resolved.

### Task 1.1 — Fix all `datetime.utcnow()` → `datetime.now(tz=UTC)` (P1)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/domain/models.py:1-452` | All 23 `datetime.utcnow` calls | Replace `datetime.utcnow` with `datetime.now(tz=timezone.utc)`. Add `from datetime import datetime, timezone` at top. |
| `src/domain/models.py:7` | Import | Change to `from datetime import datetime, timezone` |
| `src/domain/models.py:446` | `event_id` generation | Replace `datetime.utcnow().timestamp()` with `time.time()` or a monotonic counter — timestamps are not unique enough for IDs. |
| `src/domain/ports.py:43,78` | Type annotations | Change `finished_at: Optional[str]` → `Optional[datetime]` and `last_seen: Optional[str]` → `Optional[datetime]` to match model types. |
| `src/auth_middleware.py:244` | Key expiry check | Replace `datetime.utcnow()` with `datetime.now(tz=timezone.utc)` |

**Architecture note:** Domain models must produce timezone-aware datetimes. The infrastructure layer (job_repository.py) already uses `datetime.now(timezone.utc)` on lines 113, 123 for UPDATEs — this confirms the domain should match. After this fix, all DB timestamps will be consistent UTC+00:00 ISO strings.

**Verification:**
```bash
python -m pytest tests/integration/test_job_lifecycle.py -v
python -c "from src.domain.models import Job; j = Job(job_id='t', url='http://x.com'); assert j.created_at.tzinfo is not None"
```

### Task 1.2 — Fix `hash()` non-determinism in OverrideStrategy (P1)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/extractors/strategies/override.py:39-61` | `build_schema()` | Replace `hash(tuple(...))` with `hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()[:16]` |

**Architecture note:** `hash()` is randomized per Python process (PYTHONHASHSEED). Using `hashlib.sha256` produces deterministic, cross-process stable IDs. This is the same pattern already used by `ExtractedRecord.compute_identity_hash()` in `models.py:340-343` — follow that precedent.

**Verification:**
```bash
python -m pytest tests/test_override_strategy.py -v
PYTHONHASHSEED=123 python -c "from src.extractors.strategies.override import OverrideStrategy; ..."  # run twice, assert same ID
```

### Task 1.3 — Add `version` field to `Job` for optimistic concurrency (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/domain/models.py:38-66` | `Job` class | Add `version: int = Field(default=1)` to Job model |
| `src/domain/ports.py:21-24` | `update_job_state` | Add `*, expected_version: int` parameter |
| `src/infrastructure/repositories/job_repository.py:108-119` | `update_job_state` | Change SQL to `UPDATE jobs SET ... WHERE job_id = ? AND version = ?`. Increment version. Return None if no row matched (version conflict). |
| `src/infrastructure/repositories/job_repository.py:15-31` | `CREATE_JOBS_TABLE` | Add `version INTEGER NOT NULL DEFAULT 1` column |

**Architecture note:** The port already says "Atomically transition" — this makes it true. The caller retries on `None` return (version conflict). Follow the existing pattern: the repository returns `Optional[Job]` and `None` already means "not found"; now it also means "version conflict."

**Verification:**
```bash
python -m pytest tests/integration/test_job_lifecycle.py -v
# Add test: two concurrent updates, one should get None
```

### Task 1.4 — Add domain primitives for data lifecycle (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/domain/models.py:38-66` | `Job` class | Add `retention_days: Optional[int] = Field(default=None)` and `deleted_at: Optional[datetime] = Field(default=None)` |
| `src/domain/models.py:322-343` | `ExtractedRecord` class | Add `data_classification: str = Field(default="public")` (enum: public/pii/sensitive) and `deleted_at: Optional[datetime] = Field(default=None)` |
| `src/domain/models.py:17-36` | `JobState` enum | Add `DELETED = "DELETED"` state, add to `can_transition_to`: any terminal state → DELETED |
| `src/domain/ports.py:10-50` | `JobRepository` | Add `soft_delete_job(job_id: str) → Optional[Job]` and `purge_expired_jobs() → int` |
| `src/domain/ports.py:53-81` | `RecordRepository` | Add `soft_delete_record(record_id: str) → Optional[ExtractedRecord]` and `purge_expired_records() → int` |

**Architecture note:** Add the minimum domain surface now. Full implementation (DB migrations, cleanup workers) comes in Phase 5. This prevents Phase 5 from requiring domain changes retroactively.

**Verification:**
```bash
python -m py_compile src/domain/models.py src/domain/ports.py
```

### Task 1.5 — Add `idempotency_key` to API contract (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/domain/models.py:38-66` | `Job` class | Add `idempotency_key: Optional[str] = Field(default=None, description="Client-supplied dedup key")` |
| `main.py` | POST /jobs endpoint | Accept `Idempotency-Key` header. Check `job_repo.get_by_idempotency_key(key)` before creating. Return existing job if found (200 with existing body, not 201). |
| `src/domain/ports.py:10-15` | `JobRepository` | Add `get_by_idempotency_key(key: str) → Optional[Job]` |
| `src/infrastructure/repositories/job_repository.py:15-31` | Schema | Add `idempotency_key TEXT UNIQUE` column, index |

**Architecture note:** This pattern is already well-established in the codebase: `OutboxEvent.event_id` and `QueueMessage.message_id` are idempotency keys. Extend the same pattern to the API boundary.

**Verification:**
```bash
python -m pytest tests/integration/test_job_lifecycle.py -v
# Add test: POST same Idempotency-Key twice → 200 with same job
```

---

## Phase 2: Observability Backbone (3 days)

**Why second:** You can't fix what you can't see. Before touching resilience and security, tracing and metrics must work end-to-end. Every subsequent phase adds monitoring for the feature it builds.

### Task 2.1 — Propagate correlation IDs from API → Queue → Workers (P1)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/domain/models.py:89-103` | `QueueMessage` | Ensure `correlation_id` field is always populated (already exists at line 97) |
| `main.py` | POST /jobs handler | After `get_request_id()`, set `job.correlation_id = request_id` before persisting. Pass to `QueueMessage(correlation_id=...)` when enqueuing. |
| `worker_scraper.py` | `process_message()` | Extract `correlation_id` from `QueueMessage`, set via `set_request_id(correlation_id)` at start of processing |
| `worker_processor.py` | `process_message()` | Same pattern |
| `worker_reporter.py` | `process_message()` | Same pattern |
| `src/infrastructure/logger_config.py` | Log formatter | Add `correlation_id` as a structured field in the JSON log format, sourced from `get_request_id()` ContextVar |
| `src/infrastructure/middleware/correlation.py:11` | `_request_id_var` | Export a `set_request_id(value: str)` function alongside the existing `get_request_id()` |

**Architecture note:** The `QueueMessage` model already has `correlation_id` (line 97). The `Job` model already has `correlation_id` (line 51). The infrastructure exists — it's just not wired. The fix is to ensure: (1) API sets it on Job + QueueMessage, (2) workers extract it from QueueMessage and set the ContextVar, (3) logger includes it in every entry.

**Verification:**
```bash
python -m pytest tests/test_correlation_middleware.py -v
# Add integration test: POST /jobs → read worker log → assert same correlation_id
```

### Task 2.2 — Add structured log context (job_id, correlation_id) consistently (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/infrastructure/logger_config.py` | Entire file | Add `logging.LoggerAdapter` or a custom filter that injects `correlation_id` and `job_id` from ContextVars into every log record |
| `src/extractors/universal_strategy.py:73,182` | Error handling | Replace `logger.debug(...)` with `logger.warning("JSON-LD parse failed", extra={"field": "jsonld", "error": str(e)})` — structured, not free-text |
| `src/extractors/strategies/google_maps.py:301` | Error handling | Add per-field error reporting: catch inside the field loop, not around all fields. Log each failed field separately with field name. |
| `src/infrastructure/ai/client.py:112` | Error logging | Add `extra={"provider": "openai", "operation": "enrich"}` to structured log |

**Architecture note:** The project already has `python-json-logger` configured. The gap is that log calls don't pass structured `extra` dicts. Use a `LoggerAdapter` to auto-inject correlation context so individual call sites don't need to be aware.

**Verification:**
```bash
python -m pytest tests/test_extraction_pipeline.py -v
# Manually inspect log output for structured JSON fields
```

### Task 2.3 — Add DLQ depth metric + alert (P1)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/infrastructure/queues/stream_queue.py` | `push_to_dlq()` | After pushing to DLQ, call `metrics_tracker.gauge("dlq_depth", stream_name, value)` |
| `src/infrastructure/monitoring/observability.py` | `metrics_tracker` | Add `gauge()` method if not present (currently has `increment`) |
| `src/config_settings.py` | SLO settings | Add `DLQ_DEPTH_WARNING_THRESHOLD = 10` and `DLQ_DEPTH_CRITICAL_THRESHOLD = 50` |
| `src/infrastructure/notifications/notifier.py` | Alert dispatch | Add alert channel for DLQ threshold crossing |

**Verification:**
```bash
python -m pytest tests/test_stream_queue.py tests/test_resilience_oom_dlq.py -v
```

### Task 2.4 — Add pool utilization metrics (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/infrastructure/browser/pool.py` | `acquire()` / `release()` | Track `_active_contexts` / `_pool_size`, emit gauge `browser_pool_utilization` |
| `src/config_settings.py` | DB settings | After DB pool size config, add periodic logging of pool stats via SQLAlchemy events |
| `src/infrastructure/monitoring/observability.py` | `metrics_tracker` | Ensure gauge metrics are exported via OpenTelemetry |

**Verification:**
```bash
python -m pytest tests/test_increment_modules.py -v
```

### Task 2.5 — Add per-job cost tracking (P3)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/infrastructure/ai/client.py` | `enrich()`, `generate_overlay()`, `heal_selector()` | After each AI call, record `tokens_used` and `model` in a cost log or metric |
| `src/infrastructure/providers/enrichment_provider.py:63-64` | Gemini call | Same — record token count |
| `src/domain/models.py` | `Job` or `JobAttempt` | Add `ai_tokens_used: int = 0` and `ai_cost_estimate: float = 0.0` |
| `src/infrastructure/repositories/job_repository.py` | `update_attempt()` | Accept + persist token/cost fields |

**Architecture note:** This is Phase 2 because it informs every later decision ("is this AI call worth the cost?"). The data collection is cheap — just increment counters.

**Verification:**
```bash
python -m pytest tests/test_cache.py -v
# Check that AI mock records token counts
```

---

## Phase 3: Resilience Core (5 days)

**Why third:** With observability in place, we can safely add resilience mechanisms knowing we'll detect when they trigger. This phase addresses the "first 3AM page" scenarios.

### Task 3.1 — Job heartbeat + stale job reaper (P1)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/domain/models.py:38-66` | `Job` class | Add `last_heartbeat_at: Optional[datetime] = Field(default=None)` |
| `src/domain/ports.py:10-50` | `JobRepository` | Add `heartbeat(job_id: str) → None` and `find_stale_jobs(stale_seconds: int) → List[Job]` |
| `src/infrastructure/repositories/job_repository.py:15-31` | Schema | Add `last_heartbeat_at TEXT` column |
| `src/infrastructure/repositories/job_repository.py:108-119` | `update_job_state` | Add `heartbeat()`: `UPDATE jobs SET last_heartbeat_at = ? WHERE job_id = ?` |
| `src/infrastructure/repositories/job_repository.py` | New method | `find_stale_jobs()`: `SELECT * FROM jobs WHERE state = 'RUNNING' AND last_heartbeat_at < ?` |
| `worker_scraper.py:53-59` | Processing loop | After each successful page fetch, call `await self.job_repo.heartbeat(job.job_id)` |
| `worker_processor.py:53-59` | Processing loop | Same heartbeat pattern during extraction |
| `boot.py` or new `worker_reaper.py` | New component | **Stale Job Reaper**: runs every 60 seconds. Queries `find_stale_jobs(stale_seconds=120)`. Transitions them to FAILED with error "Job timed out — worker unresponsive". |

**Architecture note:** This is the #1 reliability improvement. The reaper is a new optional worker — it can run as a separate process or as a background task in `main.py`'s lifespan. It follows the same pattern as `StrategySelector.run_forever()` in `main.py:78`.

**Verification:**
```bash
python -m pytest tests/integration/test_job_lifecycle.py -v
# Add test: create RUNNING job with old heartbeat → reaper transitions to FAILED
```

### Task 3.2 — Consumer-side message_id dedup (P1)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/infrastructure/queues/stream_queue.py` | Consumer loop | Before processing, check `SEEN_IDS` set (TTL-based, or Valkey SET with TTL). If `message_id` already processed, ACK and skip. |
| `worker_scraper.py` | `process_message()` | Call `await self.stream_queue.mark_processed(message.message_id)` after successful processing |
| `worker_processor.py` | `process_message()` | Same |
| `worker_reporter.py` | `process_message()` | Same |

**Architecture note:** The `QueueMessage.message_id` field (line 95) already documents "Unique UUID for deduplication" — this task makes it true. Use a Valkey SET with `SETEX` for O(1) lookup with automatic TTL (e.g., 24h — long enough to cover any reasonable retry window).

**Verification:**
```bash
python -m pytest tests/test_stream_queue.py -v
# Add test: push same message_id twice → second is skipped
```

### Task 3.3 — Per-field isolation in Google Maps `_entry_to_data` (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/extractors/strategies/google_maps.py:198-304` | `_entry_to_data()` | Replace monolithic `try/except Exception` with per-field try/except. Each field extraction catches its own errors, logs the field name + error, and continues to the next field. Return partial results with a `_extraction_errors: [...]` metadata field listing which fields failed. |

**Architecture note:** This follows the pattern already used by `OverrideStrategy._resolve_value()` (override.py:36-37) which catches per-field. The UniversalExtractionStrategy JSON-LD extraction already handles partial results — `_entry_to_data` should match.

**Verification:**
```bash
python -m pytest tests/test_google_maps_strategy.py -v
# Add test: corrupt one field in mock JSON → 16/17 fields still extracted
```

### Task 3.4 — Outbox relay retry with backoff (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/infrastructure/outbox_relay.py` | Main loop | Add exponential backoff between retries (1s, 2s, 4s, 8s, max 60s). Add jitter (±25%). Add circuit breaker: if Valkey is down for >5 consecutive failures, pause relay for 30s. |

**Architecture note:** The AI client (`ai/client.py:100-107`) already has a circuit breaker pattern. Follow the same approach. The `OutboxRepository.mark_failed()` already tracks `retry_count` — use that to drive backoff.

**Verification:**
```bash
python -m pytest tests/test_outbox_repository.py -v
# Add test: Valkey unavailable → relay retries with backoff → succeeds on reconnect
```

### Task 3.5 — Dead man's switch for scheduled tasks (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `main.py:78` | `strategy_selector.run_forever()` | After each successful cycle, set a Valkey key `heartbeat:strategy_selector` with TTL 7200 (2× interval). |
| `src/application/strategy_selector.py` | `run_forever()` | Accept optional `heartbeat_key` parameter |
| `src/infrastructure/notifications/notifier.py` | New alert | "Dead man's switch: strategy_selector has not refreshed in 2 hours" |
| New: `worker_watchdog.py` | Monitor | Periodic check of all heartbeat keys; alert on any expired key |

**Architecture note:** Use Valkey `SETEX` for automatic expiry. The watchdog can be a simple background task in `main.py`'s lifespan or a separate lightweight worker. Pattern: set key with TTL after each success → watchdog checks key existence → alert if missing.

**Verification:**
```bash
python -m pytest tests/test_increment_modules.py -v
# Add test: skip strategy refresh → watchdog detects missing heartbeat
```

### Task 3.6 — Distributed lock for scheduled tasks (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/infrastructure/queues/stream_queue.py` | New method | `acquire_lock(lock_name: str, ttl_seconds: int) → bool` using `SET NX EX` |
| `src/application/strategy_selector.py` | `run_forever()` | Before each cycle, `await acquire_lock("strategy_refresh", ttl=300)`. Skip cycle if lock not acquired. |
| `src/infrastructure/outbox_relay.py` | Main loop | Same pattern: `acquire_lock("outbox_relay", ttl=60)` |

**Architecture note:** This prevents the dual-instance problem from Find 8.1. Valkey `SET key value NX EX ttl` provides exactly-once lock acquisition. The lock auto-expires if the holder crashes (TTL).

**Verification:**
```bash
# Integration test: two relay instances → only one processes outbox
```

---

## Phase 4: Security Hardening (4 days)

**Why fourth:** Security fixes depend on domain models (Phase 1) and observability (Phase 2) being correct. PII redaction specifically needs the data classification field from Task 1.4.

### Task 4.1 — PII redaction before AI API calls (P1)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/security/input_sanitizer.py` | New function | `redact_pii(data: dict, classification_map: dict) → dict` — redacts fields marked as "pii" or "sensitive" before sending to AI |
| `src/infrastructure/ai/client.py:63-64,119,161,188` | All AI call sites | Call `redact_pii()` on data before `json.dumps()` for the prompt. Pass `classification_map` from the extraction schema. |
| `src/infrastructure/providers/enrichment_provider.py:63-64` | Gemini call | Same |
| `src/extractors/strategies/google_maps.py:297-299` | Phone extraction | Mark phone field as `classification: "pii"` in the extracted record metadata |
| `src/extractors/strategies/google_maps_place.py:132-138` | Reviews extraction | Mark author_name as `classification: "pii"` |

**Architecture note:** Build on the existing `input_sanitizer.py` module. The redaction replaces PII fields with `[REDACTED: <field_type>]` tokens that preserve the prompt structure without exposing data.

**Verification:**
```bash
python -m pytest tests/test_security_input_sanitizer.py -v
# Add test: record with phone + email → AI prompt has [REDACTED: phone] [REDACTED: email]
```

### Task 4.2 — Production guard for DEMO_API_KEY (P1)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/auth_middleware.py:219-228` | Demo key bypass | Add `if os.environ.get("ENVIRONMENT") == "production": raise RuntimeError("DEMO_API_KEY is not allowed in production")`. Or simply: only activate demo key when `ENVIRONMENT` is explicitly `development`. |
| `src/config_settings.py` | Settings class | Add `ENVIRONMENT: str = "development"` with validator that rejects `"production"` when `DEMO_API_KEY` is set |

**Architecture note:** Fail-closed. If the code runs in production with a demo key, refuse to start. This is a one-line guard that prevents the P1 scenario.

**Verification:**
```bash
ENVIRONMENT=production DEMO_API_KEY=test python -c "from src.auth_middleware import api_key_manager; ..."  # should raise
```

### Task 4.3 — Persistent API key store (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/auth_middleware.py:74-188` | `ApiKeyManager` | Add SQLite-backed persistence. On `initialize()`, load keys from DB. On `generate_key()`, write to DB. On `revoke_key()`, update DB. |
| `src/infrastructure/repositories/` | New file | `api_key_repository.py` — CRUD for API keys with hashed storage |
| `src/domain/ports.py` | New protocol | `ApiKeyRepository` port |

**Architecture note:** Keys remain hashed (SHA-256). The in-memory dict stays as a hot cache; the DB is the source of truth. On startup, load from DB → populate cache. This follows the existing repository pattern.

**Verification:**
```bash
python -m pytest tests/ -k "api_key" -v
# Add test: generate key → restart service → key still works
```

### Task 4.4 — API key expiry notification + rotation (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/auth_middleware.py:244-248` | Expiry check | Add `X-Key-Expiring-Soon` response header when key expires within 7 days |
| `src/auth_middleware.py` | `ApiKeyManager` | Add `rotate_key(old_key_hash: str) → ApiKey` — generates new key, revokes old key after grace period |
| `main.py` | New endpoint | `POST /auth/rotate` — accepts current key, returns new key |

**Architecture note:** The 7-day warning header allows automated clients to detect and rotate before expiry. The rotation endpoint follows the same auth pattern as other endpoints.

**Verification:**
```bash
python -m pytest tests/ -k "auth" -v
```

### Task 4.5 — Rate-limit auth failures (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/auth_middleware.py:195-256` | `verify_api_key()` | Add per-IP rate limiting on failed auth attempts (e.g., 10/minute). Use Valkey `INCR` + `EXPIRE`. Return 429 on excess failures with same response shape as success (to prevent enumeration). |
| `src/auth_middleware.py:238-248` | Key validation | Unify 401 vs 403 responses: always return 401 with generic message "Invalid or expired API key" regardless of whether key is invalid, expired, or revoked. |

**Architecture note:** The current code distinguishes invalid key (401 after loop) from expired key (403 at line 244-248) and revoked key (403 at line 238-242). This allows attackers to enumerate valid keys. Unify to a single 401 response with identical timing (add a constant-time comparison or fixed sleep).

**Verification:**
```bash
python -m pytest tests/ -k "auth" -v
# Add test: 11 failed auths from same IP → 429
# Add test: invalid key vs expired key → same response code, same body shape
```

### Task 4.6 — Docker security hardening (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `Dockerfile` | After `RUN playwright install` | Add `RUN useradd -m -s /bin/bash scraper && chown -R scraper:scraper /app` and `USER scraper` |
| `Dockerfile` | Final stage | Add `RUN chmod -R 555 /app` (read-only app code) |

**Verification:**
```bash
docker build -t spacescraper . && docker run --rm spacescraper whoami  # should output "scraper"
```

### Task 4.7 — DNS rebinding mitigation for SSRF (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/security/ssrf_guard.py:35-80` | `validate_outbound_url()` | Add `pin_ip` step: after resolving, pass the resolved IP to the HTTP client to force connection to the validated IP (prevent re-resolution). Use `httpx` with explicit `transport` that connects to the pre-resolved IP. |
| `src/infrastructure/http_client.py` | `fetch()` | Accept optional `pinned_ip: str` parameter. If provided, use it instead of DNS resolution. |

**Architecture note:** The code already acknowledges the TOCTOU gap (lines 31-34). The fix: resolve once, use the resolved IP for the actual connection. This requires `httpx` transport customization — set the `Host` header to the original hostname, but connect to the pinned IP.

**Verification:**
```bash
python -m pytest tests/test_security_ssrf_guard.py -v
# Add test: DNS rebinding simulation (mock DNS that changes between calls)
```

---

## Phase 5: Data Lifecycle (3 days)

**Why fifth:** Requires domain model changes from Phase 1 (Task 1.4). Lower blast radius than resilience/security — data accumulates slowly.

### Task 5.1 — Soft delete implementation (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/infrastructure/repositories/job_repository.py` | New method | `soft_delete_job()`: `UPDATE jobs SET state = 'DELETED', deleted_at = ?, updated_at = ? WHERE job_id = ? AND state IN ('SUCCEEDED', 'FAILED', 'CANCELLED')` |
| `src/infrastructure/repositories/record_repository.py` | New method | `soft_delete_record()`: `UPDATE records SET deleted_at = ? WHERE record_id = ?` |
| `src/infrastructure/repositories/job_repository.py:130-147` | `list_jobs()` | Exclude `state = 'DELETED'` by default. Add `include_deleted: bool = False` parameter. |
| `src/infrastructure/repositories/record_repository.py` | `list_records()` | Exclude `deleted_at IS NOT NULL` by default. |
| `main.py` | New endpoints | `DELETE /jobs/{job_id}` → soft-delete. `DELETE /records/{record_id}` → soft-delete. |

**Verification:**
```bash
python -m pytest tests/integration/test_job_lifecycle.py -v
# Add test: DELETE job → state=DELETED → GET returns 404 → list excludes
```

### Task 5.2 — Data retention TTL + cleanup job (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/infrastructure/repositories/job_repository.py` | New method | `purge_expired_jobs()`: Hard-delete jobs where `deleted_at IS NOT NULL AND deleted_at < now - retention_days` |
| `src/infrastructure/repositories/record_repository.py` | New method | `purge_expired_records()`: Hard-delete records where `deleted_at IS NOT NULL AND deleted_at < now - 90_days` |
| `boot.py` or `main.py` lifespan | New background task | Runs daily: calls `purge_expired_jobs()` and `purge_expired_records()` |
| `src/infrastructure/artifact_store.py` | New method | `purge_orphaned_artifacts()`: Delete artifact files not referenced by any non-deleted job |

**Architecture note:** Two-phase deletion: soft-delete → hard-delete after retention. This satisfies GDPR "right to erasure" while allowing a grace period for accidental deletions. The daily cleanup is a simple cron-like background task.

**Verification:**
```bash
# Add test: soft-delete job with retention=1 → advance clock 2 days → hard-deleted
```

### Task 5.3 — GDPR erasure endpoint (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `main.py` | New endpoint | `POST /gdpr/erasure` — accepts `{"records": ["id1", "id2"]}` or `{"url_pattern": "example.com/*"}`. Soft-deletes matching records. Returns count of affected records. |

**Architecture note:** This is a compliance requirement. The endpoint should require an admin-tier API key (ENTERPRISE tier). It should log an audit event for every erasure request.

**Verification:**
```bash
# Add integration test: POST /gdpr/erasure → records soft-deleted → GET returns 404
```

### Task 5.4 — Migration rollback capability (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `migrate_sqlite_to_postgres.py` | Entire file | Add `--dry-run` flag (validate + count, don't write). Add `--transactional` flag (wrap in BEGIN/COMMIT, rollback on error). Add progress logging every 1000 rows. Add `--verify` mode (compare row counts between SQLite and PostgreSQL). |

**Architecture note:** This is a safety net for the one-way migration. Not a full rollback system — just enough guardrails to prevent partial-data disasters.

**Verification:**
```bash
python migrate_sqlite_to_postgres.py --dry-run  # should output counts, no writes
```

---

## Phase 6: Operational Excellence (4 days)

**Why sixth:** These are process/quality improvements. They don't block functionality but dramatically improve incident response and maintainability.

### Task 6.1 — Operational runbooks (P2)

**Files to change (new):**

| File | Content |
|------|---------|
| `docs/runbooks/README.md` | Index of runbooks |
| `docs/runbooks/scraper-down.md` | Scraper worker crash → diagnosis → recovery steps |
| `docs/runbooks/dlq-alert.md` | DLQ depth alert → how to inspect + replay DLQ messages |
| `docs/runbooks/slo-breach.md` | SLO AutoRollback triggered → how to investigate + override |
| `docs/runbooks/api-key-expiry.md` | Key rotation procedure |
| `docs/runbooks/valkey-outage.md` | Valkey unavailability → impact → recovery |
| `docs/runbooks/db-migration.md` | Database migration procedure + rollback |

**Each runbook template:**
```
# [Alert Name]
## Symptoms: (what the on-call sees)
## Impact: (what's broken for users)
## Diagnosis: (commands to run, dashboards to check)
## Mitigation: (immediate stop-the-bleeding steps)
## Resolution: (permanent fix steps)
## Escalation: (when to call whom)
```

### Task 6.2 — Architecture Decision Records (P3)

**Files to change (new):**

| File | Decision |
|------|----------|
| `docs/adr/001-valkey-streams-over-kafka.md` | Why Valkey Streams for lightweight deployment |
| `docs/adr/002-sqlite-over-postgres.md` | Why SQLite default, PostgreSQL for enterprise |
| `docs/adr/003-dual-pipeline-architecture.md` | Why old pipeline.py coexists with extraction_pipeline.py |
| `docs/adr/004-content-addressed-storage.md` | Why SHA256 artifact storage |
| `docs/adr/005-google-maps-array-indices.md` | Document the reverse-engineered indices with their semantic meaning |

### Task 6.3 — Consumer lag monitoring + backpressure signal (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/infrastructure/queues/stream_queue.py` | New method | `get_consumer_lag(stream_name: str, group_name: str) → int` using `XINFO GROUPS` |
| `src/infrastructure/monitoring/observability.py` | `metrics_tracker` | Add gauge `stream_consumer_lag` per stream |
| `src/infrastructure/notifications/notifier.py` | Alerts | Alert when lag > 1000 for > 5 minutes |
| `worker_scraper.py` | Processing loop | Check processor lag before enqueuing. If lag > threshold, apply adaptive backpressure (add delay between scrapes). |

**Architecture note:** Valkey Streams provides `XINFO GROUPS` which returns the `lag` field per consumer. Use this to detect when the processor is falling behind.

**Verification:**
```bash
python -m pytest tests/test_stream_queue.py -v
# Add test: simulate processor lag → scraper detects and slows down
```

### Task 6.4 — Cumulative timeout budget enforcement (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/config_settings.py` | Settings | Add `JOB_TIMEOUT_SECONDS: int = 300` (matching pipeline_config.yaml SLA) |
| `worker_scraper.py` | `process_message()` | Wrap processing in `asyncio.wait_for(..., timeout=JOB_TIMEOUT_SECONDS)`. On timeout, transition job to FAILED with "Job exceeded timeout." |
| `worker_processor.py` | `process_message()` | Same pattern |

**Architecture note:** `asyncio.wait_for` raises `asyncio.TimeoutError` — catch it, transition job state, and move to next message. This prevents a single stuck job from blocking the worker indefinitely.

**Verification:**
```bash
python -m pytest tests/ -k "timeout" -v
# Add test: job with infinite loop → worker times out after 300s → job FAILED
```

### Task 6.5 — Resource exhaustion alerts (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/infrastructure/monitoring/observability.py` | `metrics_tracker` | Add `gauge("disk_usage_percent")` and `gauge("db_pool_available")` |
| `src/infrastructure/artifact_store.py` | `store()` | Before write, check `shutil.disk_usage()`. If < 10% free, emit metric + log warning. |
| `src/config_settings.py` | Settings | Add `DISK_USAGE_WARNING_PERCENT = 80`, `DISK_USAGE_CRITICAL_PERCENT = 90` |
| `src/infrastructure/notifications/notifier.py` | Alerts | Alert on disk > 90% or DB pool exhausted |

**Verification:**
```bash
python -m pytest tests/test_artifact_store.py -v
```

### Task 6.6 — Error handling: structured logging instead of silent swallows (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/extractors/universal_strategy.py:73` | `except (JSONDecodeError, AttributeError): pass` | Change to `except (JSONDecodeError, AttributeError) as e: logger.info("JSON-LD not found or malformed", extra={"url": url, "error": str(e)})` |
| `src/extractors/universal_strategy.py:182` | `except Exception as e: logger.debug(...)` | Change log level to `warning` and add structured fields |
| `src/extractors/strategies/google_maps.py:301` | Single try/except | Already addressed in Task 3.3 |

**Architecture note:** Debug-level logging for known-expected conditions (JSON-LD absent is normal, not an error) should be `info`. Unexpected exceptions should be `warning` or `error`. Use structured `extra` dicts.

**Verification:**
```bash
python -m pytest tests/test_extraction_pipeline.py tests/test_extractors_generic.py -v
```

### Task 6.7 — Google Maps indices documentation (P1)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/extractors/strategies/google_maps.py:198-304` | `_entry_to_data()` | Add docstring mapping each array index to its semantic meaning: `arr[11] = "category arrays"`, `arr[14] = "place ID"`, `arr[30] = "latitude"`, `entry[178][0][0] = "review count"` |
| `docs/adr/005-google-maps-array-indices.md` | New | Full documentation of the Google Maps internal JSON structure, how indices were discovered, and how to update them if the format changes |

---

## Phase 7: Architecture Cleanup (4 days)

**Why last:** These are refactoring/cleanup tasks. They improve maintainability but don't fix active bugs. Do them after the system is stable and observable.

### Task 7.1 — Strategy interface: enforce ABC inheritance (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/extractors/strategies/google_maps.py` | Class definition | `class GoogleMapsStrategy(BaseExtractionStrategy):` |
| `src/extractors/strategies/google_maps_place.py` | Class definition | `class GoogleMapsPlaceStrategy(BaseExtractionStrategy):` |
| `src/extractors/strategies/generic.py` | Class definition | `class GenericStrategy(BaseExtractionStrategy):` |
| `src/extractors/strategies/override.py` | Class definition | `class OverrideStrategy(BaseExtractionStrategy):` |
| `src/extractors/base_extractor.py` | `BaseExtractionStrategy` | Mark `extract()` as `@abstractmethod` |

**Architecture note:** This is a pure-code-health change. No behavioral difference — the strategies already match the interface by duck-typing. Making it explicit prevents future drift.

**Verification:**
```bash
python -m pytest tests/test_extraction_pipeline.py tests/test_google_maps_strategy.py tests/test_override_strategy.py -v
```

### Task 7.2 — Fix `@lru_cache` on instance method (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/infrastructure/ai/client.py:211-219` | `_compute_embedding_cached()` | Remove `@lru_cache`. Replace with explicit cache using a module-level `dict` or `cachetools.TTLCache` keyed by `hashlib.sha256(text.encode()).hexdigest()`. |

**Architecture note:** The `@lru_cache` on an instance method includes `self` in the cache key, preventing any cache hit. The fix: use a class-level or module-level cache keyed by content hash. This matches the content-addressed pattern already used in `ArtifactStore`.

**Verification:**
```bash
python -c "
from src.infrastructure.ai.client import ai_orchestrator
# Call _compute_embedding_cached twice with same text → should hit cache on second call
"
```

### Task 7.3 — Reuse `DeterministicExtractionPipeline` instance (P3)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/extractors/strategies/generic.py:26-28` | `extract()` | Accept `pipeline: DeterministicExtractionPipeline` in `__init__`. Store as `self._pipeline`. Reuse across calls. |
| `worker_processor.py:48-49` | Initialization | Create a single `DeterministicExtractionPipeline` instance and pass it to `GenericStrategy`. |

**Architecture note:** This also fixes the DI bypass — `GenericStrategy` currently creates its own pipeline, bypassing the composition root. Follow the `bootstrap.py` pattern.

**Verification:**
```bash
python -m pytest tests/test_extractors_generic.py -v
```

### Task 7.4 — Deprecate legacy pipeline.py and redis_worker.py (P3)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/application/pipeline.py` | Top of file | Add deprecation warning: `warnings.warn("pipeline.py is deprecated; use extraction_pipeline.py", DeprecationWarning)` |
| `src/infrastructure/queues/redis_worker.py` | Top of file | Add deprecation warning: `warnings.warn("redis_worker.py is deprecated; use stream_queue.py", DeprecationWarning)` |
| `worker_processor.py:42` | Pipeline instantiation | Replace `DataPipeline` with `DeterministicExtractionPipeline` |
| `worker_scraper.py:46` | Queue worker | Replace `RedisQueueWorker` with `RedisStreamQueue` |
| `README.md` | Architecture docs | Remove references to legacy pipeline, document the new pipeline |

**Architecture note:** The project already has new paths (`extraction_pipeline.py`, `stream_queue.py`) running alongside old ones (`pipeline.py`, `redis_worker.py`). This task cuts over the remaining references and adds deprecation warnings so any stray usage is visible.

**Verification:**
```bash
python -m pytest tests/ -v  # full test suite with deprecation warnings visible
```

### Task 7.5 — Unify docker-compose config drift (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `docker-compose.yml` | Services | Add profiles: `standard` vs `enterprise`. Merge `docker-compose.enterprise.yml` into a single file using Compose profiles. |
| `docker-compose.enterprise.yml` | — | Delete after merge. |

**Architecture note:** Docker Compose profiles allow `docker compose --profile enterprise up` vs `docker compose up` (standard). This eliminates the dual-file maintenance burden while keeping deployment simple.

**Verification:**
```bash
docker compose config  # validate merged config
docker compose --profile enterprise config  # validate enterprise config
```

### Task 7.6 — Graceful degradation: HTTP-only fallback for Playwright failures (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/smart_crawler.py` | `fetch()` | Add fallback: if Playwright fetch fails (pool exhausted, browser crash), fall back to raw HTTP GET via `httpx`. Mark result with `fetch_method: "http_fallback"` so the processor knows it's a limited extraction. |
| `src/infrastructure/browser/engine.py` | `fetch_page()` | Raise a specific `BrowserUnavailableError` (subclass of `ScrapeFailure`) when pool is exhausted, so the caller can distinguish from page-level errors. |
| `worker_scraper.py` | `process_message()` | Catch `BrowserUnavailableError` → fall back to HTTP fetch → publish payload with degraded flag |

**Verification:**
```bash
python -m pytest tests/test_cache.py -v
# Add test: browser pool full → HTTP fallback used → payload has fetch_method=fallback
```

### Task 7.7 — License audit + dependency lock (P1, P3)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `requirements.lock` | New file | `pip freeze --require-hashes > requirements.lock` for the core dependencies |
| `requirements-enterprise.lock` | New file | Same for enterprise dependencies |
| `docs/LICENSE_AUDIT.md` | New file | Run `pip-licenses` on both requirement sets. Document any copyleft licenses. Flag packages with no license. |
| `Dockerfile` | `pip install` line | Add `--require-hashes -r requirements.lock` |

**Architecture note:** The lock file ensures reproducible builds. `--require-hashes` protects against package tampering. The license audit is a one-time check that should be repeated when dependencies change.

**Verification:**
```bash
pip install --require-hashes -r requirements.lock  # should succeed
pip-licenses --from=requirements.txt  # check for GPL/AGPL
```

### Task 7.8 — Vertex/lock-in: AI provider abstraction layer (P3)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/infrastructure/ai/` | New file | `provider.py` — `AIProvider` protocol with `enrich()`, `generate_overlay()`, `compute_embedding()` |
| `src/infrastructure/ai/` | New file | `openai_provider.py` — existing OpenAI logic, implements `AIProvider` |
| `src/infrastructure/providers/` | Moved | `gemini_provider.py` — existing Gemini logic, implements `AIProvider` |
| `src/infrastructure/ai/client.py` | Refactor | `AIOrchestrator` accepts `AIProvider` in `__init__`. Routes calls through the provider abstraction. |

**Architecture note:** The system already has two providers. This just formalizes the boundary. New providers (Anthropic, local LLM) implement the protocol. This is a pure refactor — no behavior change.

**Verification:**
```bash
python -m pytest tests/ -v  # full suite
python -m py_compile src/infrastructure/ai/*.py
```

### Task 7.9 — Left-pad mitigation (P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `Dockerfile` | Before `pip install` | Add `--index-url` with a configurable PyPI mirror env var |
| `scripts/vendor_deps.sh` | New file | Script to download all wheels to `vendor/` directory for offline installs |
| `docs/DEPENDENCY_POLICY.md` | New file | Policy: lock files committed, mirror URL configurable, vendor option for air-gapped deploys |

### Task 7.10 — Cost anomaly detection + cron overlap protection (P2, P2)

**Files to change:**

| File | Lines | Change |
|------|-------|--------|
| `src/infrastructure/slo_monitor.py` | `evaluate()` | Add cost-per-job trend detection: if average cost increases >50% over trailing 24h, fire alert |
| `src/application/strategy_selector.py` | `run_forever()` | Add overlap guard: at start of cycle, check if previous cycle is still running (track with `_running` boolean). If so, skip this cycle and log warning. |

---

## Summary: Effort vs Impact Matrix

| Phase | Effort | P1s Fixed | P2s Fixed | P3s Fixed | Key Outcome |
|-------|--------|-----------|-----------|-----------|-------------|
| 1. Domain Foundation | 4d | 3 | 3 | 0 | Correct data types, deterministic schemas |
| 2. Observability | 3d | 2 | 2 | 1 | End-to-end tracing, alerting foundation |
| 3. Resilience Core | 5d | 3 | 3 | 0 | Jobs survive worker crashes |
| 4. Security Hardening | 4d | 2 | 4 | 0 | PII protection, auth robustness |
| 5. Data Lifecycle | 3d | 0 | 3 | 0 | GDPR compliance, storage management |
| 6. Operational Excellence | 4d | 1 | 3 | 1 | Runbooks, backpressure, error quality |
| 7. Architecture Cleanup | 4d | 0 | 2 | 5 | Maintainability, vendor independence |
| **Total** | **~27d** | **11** | **20** | **7** | **13 P1s → 0, ship decision → READY** |

## Dependency Lock File (P1)
Immediate action before any code changes:

```bash
pip freeze --require-hashes > requirements.lock
pip-licenses --from=requirements.txt --format=markdown > docs/LICENSE_AUDIT.md
```

This can be done in parallel with Phase 1 and has zero code impact.
