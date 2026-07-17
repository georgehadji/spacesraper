# Spacescraper

Generic web-extraction service with a FastAPI control surface and Valkey-backed worker pipeline.

## Architecture

```
FastAPI -> Job store (SQLite) + Outbox -> Valkey Streams -> Scraper -> Processor -> Reporter
                                                              |            |
                                                           Artifacts   Records
```

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Start all processes
python boot.py

# Or start individually:
python main.py              # API server on :8000
python worker_scraper.py    # Scraper worker
python worker_processor.py  # Processor worker
python worker_reporter.py   # Reporter worker
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/jobs` | Submit a scraping job (returns 202) |
| GET | `/jobs/{id}` | Get job status and metadata |
| POST | `/jobs/{id}/cancel` | Cancel a queued/running job |
| GET | `/jobs/{id}/records` | Get extracted records (cursor pagination) |
| POST | `/autograph` | Generate extraction overlay from HTML |
| POST | `/auth/register` | Register a new API key |
| POST | `/overlays/{id}/promote` | Promote overlay through lifecycle |
| POST | `/records/{id}/feedback` | Submit feedback on an extracted record |
| GET | `/health` | System health with SLO status |
| GET | `/slo` | SLO alert status |
| GET | `/metrics` | Cluster metrics snapshot |

## Architecture Overview

### Data Flow
1. `POST /jobs` creates a durable `Job` record (QUEUED) + outbox event
2. Job is pushed to Valkey Stream → consumed by scraper worker
3. Scraper fetches page (HTTP or browser), stores raw HTML as content-addressed artifact
4. Raw payload goes to processor → extraction pipeline (overlay → JSON-LD → semantic HTML)
5. Extracted records persisted, job state updated to SUCCEEDED
6. Discovery events published for reporters (Slack, webhooks, file exports)

### Key Components
- **Durable state machine**: Job QUEUED → RUNNING → SUCCEEDED/FAILED/CANCELLED
- **Valkey Streams**: Consumer groups with ACK/DLQ, replaces old LIST-based queue
- **Outbox pattern**: Events persisted atomically with job creation, relayed asynchronously
- **Overlay lifecycle**: CANDIDATE → SHADOW → ACTIVE → RETIRED with human-gated promotion
- **Auto-strategy selection**: Observations → evaluator → domain profile updates (hourly)
- **SLO monitoring**: 6 SLOs with warning/critical thresholds, auto-rollback on regression
- **Content-addressed storage**: `artifacts/{sha256}` for raw HTML/JSON

### Project Structure
```
src/
  domain/          — Pure Pydantic models and protocol ports
    models.py      — All domain entities (Job, ExtractedRecord, QueueMessage, etc.)
    ports.py       — Repository protocols (JobRepository, RecordRepository, etc.)
  application/     — Use-case services
    pipeline.py         — Legacy ETL pipeline
    extraction_pipeline.py — Deterministic extraction chain
    evaluator.py         — Strategy evaluation
    strategy_selector.py — Auto-strategy selection
    exploration_policy.py — Thompson sampling exploration
    shadow_evaluator.py  — Shadow overlay evaluation
  infrastructure/  — Adapters (SQLite, Valkey, HTTP, etc.)
    repositories/       — SQLite implementations
    queues/             — Valkey Stream queue adapter
    exports/            — CSV/JSON artifact writers
    providers/          — AI enrichment providers
    artifact_store.py   — Content-addressed storage
    cache.py            — Two-level LRU+Valkey cache
    rate_limiter.py     — Per-domain concurrency budgets
    slo_monitor.py      — SLO evaluation and auto-rollback
    outbox_relay.py     — Outbox event relay service
  extractors/      — Extraction strategies
  security/        — SSRF guard, input sanitizer, CORS config
workers/           — Entry points (in project root)
  worker_scraper.py
  worker_processor.py
  worker_reporter.py
```

## Configuration

Environment variables (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `VALKEY_URL` | `valkey://localhost:6379` | Valkey connection |
| `DEMO_API_KEY` | — | Development API key |
| `GEMINI_API_KEY` | — | Gemini AI API key |
| `SLACK_WEBHOOK_URL` | — | Slack notification URL |
| `CORS_ALLOWED_ORIGINS` | localhost | CORS origins |
| `ENVIRONMENT` | development | Environment name |

## Tests

```bash
# Run all working tests
python -m pytest tests/test_security_ssrf_guard.py tests/test_security_input_sanitizer.py \
  tests/test_security_exceptions.py tests/test_correlation_middleware.py \
  tests/test_resilience_identity_hash.py tests/test_resilience_oom_dlq.py \
  tests/test_resilience_turbo_guard.py tests/test_resilience_fanout_cap.py \
  tests/test_extractors_generic.py tests/test_stream_queue.py \
  tests/test_record_repository.py tests/test_outbox_repository.py

# Run increment module tests (standalone)
python -c "exec(open('tests/test_increment_modules.py').read())"
```
