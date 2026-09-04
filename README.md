# Spacescraper

<p align="center">
  <strong>Production-grade distributed web extraction service</strong><br>
  FastAPI control surface · Playwright browser cluster · Valkey Streams pipeline · AI-powered enrichment
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/tests-257%2F257-brightgreen" alt="Tests">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
</p>

---

## Overview

Spacescraper is a horizontally scalable web extraction platform. Submit URLs via a REST API and receive structured, AI-enriched records extracted through a multi-strategy pipeline backed by headless browsers.

**Key capabilities:**

- **Headless browser extraction** — Playwright Chromium cluster with anti-fingerprinting and proxy rotation
- **Multi-strategy pipeline** — user-supplied overrides → domain-specific strategies → JSON-LD → semantic HTML fallback
- **AI enrichment** — OpenAI / Gemini integration for field translation, classification, and schema generation
- **At-least-once delivery** — transactional outbox → Valkey Streams with consumer groups, ACK, retry, and dead-letter queue
- **Auto-strategy selection** — Thompson-sampling exploration policy adapts extraction strategies per domain
- **SLO monitoring** — 6 service-level objectives with warning/critical thresholds and automatic rollback
- **Content-addressed storage** — raw HTML artifacts keyed by SHA-256 for cache validation and deduplication

## Architecture

```
                              ┌──────────────────────┐
                              │      FastAPI (:8000)   │
                              │   POST /jobs           │
                              │   GET  /jobs/{id}      │
                              │   GET  /records        │
                              │   DELETE /jobs/{id}    │
                              └─────────┬────────────┘
                                        │
                              ┌─────────▼────────────┐
                              │    SQLite / PostgreSQL │
                              │   Jobs · Records       │
                              │   Outbox · Overlays    │
                              └─────────┬────────────┘
                                        │ OutboxRelay
                              ┌─────────▼────────────┐
                              │    Valkey Streams      │
                              │   jobs_stream          │
                              │   raw_payloads_stream  │
                              │   discovery_stream     │
                              └──┬───────┬───────┬────┘
                                 │       │       │
                    ┌────────────▼─┐ ┌───▼──────────▼─┐ ┌─▼──────────────┐
                    │   Scraper    │ │   Processor     │ │   Reporter     │
                    │  (2×replica) │ │                 │ │                │
                    │              │ │  Override       │ │  Slack         │
                    │  Playwright  │ │  → JSON-LD      │ │  Webhook       │
                    │  HTTP cache  │ │  → Semantic     │ │  CSV/JSON      │
                    │  Turbo mode  │ │  → AI enrich    │ │                │
                    └──────┬───────┘ └────────┬────────┘ └────────────────┘
                           │                  │
                    ┌──────▼──────┐   ┌───────▼──────┐
                    │  Artifacts  │   │   Records     │
                    │  {sha256}   │   │  (SQLite/PG)  │
                    └─────────────┘   └──────────────┘
```

### Data Flow

1. `POST /jobs` → durable `Job` record (QUEUED) + transactional outbox event
2. OutboxRelay pushes to Valkey Stream `jobs_stream`
3. Scraper consumes → HTTP cache check (ETag / If-Modified-Since) → Playwright fetch → content-addressed artifact
4. Raw payload published to `raw_payloads_stream`
5. Processor consumes → multi-strategy extraction → schema validation → AI enrichment
6. Records persisted, job state → SUCCEEDED, discovery events published
7. Reporter consumes → Slack notifications, webhook callbacks, CSV/JSON exports

## Quick Start

### Docker (Recommended)

```bash
# Clone and start the full cluster
git clone <repo-url> && cd Spacescraper
cp .env.example .env       # edit with your API keys
docker compose up -d       # 5 services: api, scraper×2, processor, reporter, valkey

# Check health
curl http://localhost:8000/health

# Register an API key. GET /demo/key only works when DEMO_API_KEY is set in a
# development environment, so register one otherwise.
API_KEY=$(curl -sX POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "tier": "pro"}' | jq -r .api_key)

# Submit your first job
curl -X POST http://localhost:8000/jobs \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://books.toscrape.com", "target_site": "universal"}'
```

### Headless CLI (for external agents and scripts)

`cli.py` runs a scrape in a single process with no broker, no workers, and no
API. Every command writes one JSON document to stdout and all logs to stderr,
so the output can be piped straight into a parser.

```bash
python cli.py health                                  # dependency report
python cli.py scrape https://example.com --pretty     # fetch over HTTP, then extract
python cli.py scrape https://example.com --browser    # headless Chromium for JS pages
python cli.py extract --html-file page.html --url https://example.com
cat page.html | python cli.py extract --url https://example.com
python cli.py submit https://example.com              # enqueue for the worker cluster
```

Exit codes: `0` success, `1` ran but found no records, `2` usage or input error,
`3` fetch or backend failure. `--browser` needs Chromium installed once:
`python -m playwright install chromium`.

Inside Docker, without starting the cluster:

```bash
docker compose run --rm cli scrape https://example.com --pretty
```

### Enterprise Stack

```bash
docker compose -f docker-compose.enterprise.yml up -d
# Adds: PostgreSQL, Kafka, Prometheus, Grafana
```

### Local Development

```bash
pip install -r requirements.txt
python boot.py                 # starts all 4 processes
# or individually:
python main.py                 # http://localhost:8000
python worker_scraper.py       # browser-based fetcher
python worker_processor.py     # extraction pipeline
python worker_reporter.py      # exports & notifications
```

## API Reference

### Authentication

All endpoints require a Bearer token in the `Authorization` header. Keys are managed via in-memory store with SHA-256 hashing and tiered rate limiting.

```
Authorization: Bearer sk_xxxxxxxxxxxxxxxx
```

| Tier | Rate Limit | Description |
|------|-----------|-------------|
| `free` | 100 req/day | Evaluation |
| `basic` | 1,000 req/day | Light use |
| `pro` | 10,000 req/day | Production |
| `enterprise` | 100,000 req/day | High volume |

### Endpoints

#### Jobs

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/jobs` | Submit a scraping job |
| `GET` | `/jobs/{job_id}` | Get job status and metadata |
| `POST` | `/jobs/{job_id}/cancel` | Cancel a QUEUED or RUNNING job |
| `GET` | `/jobs/{job_id}/records` | Get extracted records (cursor pagination) |
| `DELETE` | `/jobs/{job_id}` | Soft-delete a job and its records |

**POST /jobs** — Submit a job

```json
{
  "url": "https://example.com/products",
  "target_site": "universal",
  "overlay": {
    "container_selector": ".product-item",
    "mappings": {
      "title": "h2.title",
      "price": ".price-tag",
      "url": "a.link"
    }
  },
  "webhook_url": "https://hooks.example.com/callback",
  "persona_id": "shadow-persona-1"
}
```

Response `202 Accepted`:
```json
{
  "status": "accepted",
  "job_id": "job_a1b2c3d4",
  "message": "Task acknowledged. Workers will process it asynchronously.",
  "cached": false
}
```

**GET /jobs/{job_id}** — Job status

```json
{
  "job_id": "job_a1b2c3d4",
  "state": "SUCCEEDED",
  "url": "https://example.com/products",
  "target_site": "universal",
  "record_count": 24,
  "correlation_id": "req_e8f3a1b2",
  "created_at": "2025-07-18T14:30:00+00:00",
  "updated_at": "2025-07-18T14:31:15+00:00"
}
```

**GET /jobs/{job_id}/records** — Paginated records

```
GET /jobs/job_a1b2c3d4/records?limit=50&cursor=rec_abc123
```

Response:
```json
{
  "records": [
    {
      "record_id": "rec_abc123",
      "record_type": "product",
      "data": {
        "title": "Widget Pro",
        "price": "29.99",
        "url": "https://example.com/products/widget-pro"
      },
      "change_type": "NEW"
    }
  ],
  "next_cursor": "rec_def456",
  "total": 24
}
```

### Health & Monitoring

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | System health with SLO status |
| `GET` | `/slo` | Detailed SLO alert breakdown |
| `GET` | `/metrics` | Cluster metrics snapshot |

### Overlays

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/autograph` | Generate extraction overlay from HTML sample |
| `POST` | `/overlays/{id}/promote` | Promote overlay through lifecycle (CANDIDATE → SHADOW → ACTIVE) |

## Configuration

All configuration via environment variables. See `.env.example` for the full template.

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `VALKEY_URL` | `valkey://localhost:6379` | Yes | Valkey connection string. `valkey://`, `valkeys://`, `redis://` and `unix://` are all accepted. |
| `AI_OPENROUTER_API_KEY` | — | For AI enrichment | OpenRouter key. The only AI credential: every model, Gemini and OpenAI included, is reached through OpenRouter. See `OPENROUTER_SETUP.md`. |
| `SLACK_WEBHOOK_URL` | — | For notifications | Slack incoming webhook URL |
| `DEMO_API_KEY` | — | Development only | Bypass key for local testing (blocked in production) |
| `ENVIRONMENT` | `development` | Yes | `development` or `production` |
| `CORS_ALLOWED_ORIGINS` | `localhost:3000,localhost:8000` | No | Comma-separated CORS origins |
| `DB_POOL_SIZE` | `5` | No | Database connection pool size |
| `REDIS_URL` | _unset_ | No | Deprecated alias for `VALKEY_URL`, still honoured for existing deployments. |
| `LOG_LEVEL` | `INFO` | No | Logging level (DEBUG, INFO, WARNING, ERROR) |

### Pipeline Tuning

Edit `pipeline_config.yaml` for advanced extraction tuning:

```yaml
extraction:
  sources: [dom_harvest, network_interception]
  validation: {schema_integrity: true, dedup: identity_hash}
orchestration:
  max_fanout: 200
  sla_seconds: 300
  browser_pool_size: 3
```

## Project Structure

```
src/
├── domain/              # Pure domain models and port interfaces
│   ├── models.py        # Pydantic models: Job, Record, QueueMessage, Overlay
│   ├── ports.py         # Repository protocols for dependency inversion
│   └── exceptions.py    # Domain exception hierarchy
├── application/         # Use-case orchestration
│   ├── extraction_pipeline.py   # Deterministic extraction chain (replaces legacy pipeline.py)
│   ├── post_processor.py        # State audit and record persistence
│   ├── strategy_selector.py     # Auto-strategy selection per domain
│   ├── evaluator.py             # Strategy evaluation engine
│   ├── exploration_policy.py    # Thompson-sampling exploration
│   └── shadow_evaluator.py      # Shadow overlay A/B evaluation
├── infrastructure/      # Adapter implementations
│   ├── repositories/    # SQLite job/record/outbox/overlay/observation repositories
│   ├── queues/          # Valkey Streams queue (consumer groups, ACK, DLQ)
│   ├── browser/         # Playwright engine, context pool, stealth persona
│   ├── ai/              # OpenAI/Gemini client with circuit breaker
│   ├── exports/         # CSV/JSON writers, Slack/webhook plugins, report generator
│   ├── middleware/      # Correlation ID middleware, observability metrics
│   ├── cache.py         # Two-level AI cache (local LRU + Valkey)
│   ├── rate_limiter.py  # Per-domain concurrency budget enforcement
│   ├── slo_monitor.py   # SLO evaluation with auto-rollback
│   └── outbox_relay.py  # Outbox → Valkey relay service
├── extractors/          # Extraction strategies
│   ├── strategies/      # Google Maps, override, generic (JSON-LD + semantic HTML)
│   └── universal_strategy.py  # Strategy dispatcher
└── security/            # SSRF guard, input sanitizer, CORS config
```

### Entry Points

| File | Role | Long-running? |
|------|------|---------------|
| `cli.py` | Headless JSON CLI for external agents | no |
| `main.py` | FastAPI REST API on `:8000` | yes |
| `worker_scraper.py` | Browser-based page fetching | yes |
| `worker_processor.py` | Extraction pipeline + record persistence | yes |
| `worker_reporter.py` | Exports, webhooks, notifications | yes |
| `boot.py` | Multi-process launcher (all 4 processes) | yes |
| `start_all.bat` | Windows launcher: health check, then `boot.py` | yes |
| `spacescraper.py` | Unified in-process tower | yes |
| `demo_run.py` | End-to-end architecture validation | no |
| `submit_url.py` | Enqueue one URL for the cluster | no |

An external agent that just wants data should use `cli.py` — it needs no broker,
no workers, and no API, and it exits with a meaningful status code.

## State Machine

Jobs follow a guarded lifecycle. Invalid transitions raise `ValueError`.

```
QUEUED ──▶ RUNNING ──▶ SUCCEEDED ──▶ DELETED
  │           │            │
  │           │            └──▶ (no further transitions)
  │           │
  ├───────────┼──▶ FAILED ──▶ QUEUED (retry) ──▶ RUNNING ...
  │           │       │
  │           │       └──▶ DEAD_LETTERED ──▶ DELETED
  │           │
  ├───────────┼──▶ CANCELLED ──▶ DELETED
  │           │
  └───────────┘

All terminal states (SUCCEEDED, FAILED, CANCELLED, DEAD_LETTERED):
  → DELETED (soft delete)
```

Extracted records track change detection via `identity_hash` (SHA-256 of raw fields, before AI mutation):

```
NEW → UPDATED → UNCHANGED
```

## Observability

### Structured Logging

All log entries include `correlation_id` for end-to-end request tracing:

```
2025-07-18T14:31:15+00:00 - [Spacescraper.Processor] - INFO - [corr=req_e8f3a1b2] - Extracted 24 records for job_a1b2c3d4
```

### Metrics

- **OpenTelemetry** traces exported via OTLP
- **Prometheus** metrics at `/metrics`
- **6 SLOs** tracked with warning/critical thresholds:
  - Extraction success rate (≥ 85%)
  - Queue age (≤ 300s)
  - Cache hit rate (≥ 30%)
  - DLQ growth rate (≤ 10/hour)
  - Block rate (≤ 10%)
  - AI cost per hour (≤ $100)

### Correlation IDs

Incoming `X-Request-ID` headers propagate through the entire pipeline: API → Outbox → Valkey → Scraper → Processor → Reporter. Check any worker log and search by `corr=<id>` to trace a job end-to-end.

## Testing

```bash
# Run the whole suite
python -m pytest tests/ -q

# Unit tests only
python -m pytest tests/test_* -q

# Integration tests
python -m pytest tests/integration/ -q

# Specific test modules
python -m pytest tests/test_cli.py -v
python -m pytest tests/test_security_input_sanitizer.py -v
python -m pytest tests/test_stream_queue.py -v
```

The test suite covers:
- **Security**: Input sanitization, SSRF guard, exception hierarchy
- **Resilience**: OOM → DLQ, fan-out capping, identity hash stability, turbo guard
- **Data**: Repositories (job, record, outbox), artifact store, cache
- **Extraction**: Pipeline, schema validation, override strategy, Google Maps strategy
- **Entrypoints**: Every module imports cleanly; the CLI's JSON contract and exit codes
- **Integration**: Job lifecycle, API gateway boot and HTTP flow, cluster scrape → process → persist

## Deployment

### Docker Compose

```bash
# Production deployment
ENVIRONMENT=production docker compose up -d --build

# Scale scraper workers
docker compose up -d --scale scraper=4

# Enterprise stack (PostgreSQL + Kafka + Prometheus + Grafana)
docker compose -f docker-compose.enterprise.yml up -d
```

### Security Hardening

- Container runs as non-root `spacescraper` user
- `DEMO_API_KEY` is **blocked** at startup when `ENVIRONMENT=production`
- PII fields (phone, email, address, SSN, bank details) are redacted before AI API calls
- SSRF protection blocks requests to private/internal IP ranges
- CORS is restricted to configured origins only (no wildcard)

### Database

Default deployment uses **SQLite** with WAL mode for concurrent reads. For production workloads, use the enterprise stack which provisions **PostgreSQL** with SQLAlchemy async support.

Migration from SQLite to PostgreSQL:
```bash
python migrate_sqlite_to_postgres.py --dry-run   # Validate first
python migrate_sqlite_to_postgres.py              # Execute migration
```

## Development

### Prerequisites

- Python 3.11+
- Valkey 7.2+ (or use Docker: `docker run -d -p 6379:6379 valkey/valkey:8-alpine`)
- Playwright Chromium (`playwright install chromium`)

### Setup

```bash
python -m venv venv && source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys
python boot.py
```

### Code Quality

```bash
# Syntax check all source files
python -m py_compile main.py src/**/*.py worker_*.py boot.py

# Run targeted tests during development
python -m pytest tests/test_extraction_schema.py -v
```

### Architecture Decisions

| Decision | Rationale |
|----------|-----------|
| **SQLite default, PostgreSQL enterprise** | Zero-config for evaluation; full ACID for production |
| **Valkey Streams over raw LIST** | Consumer groups, ACK/DLQ, pending message claiming |
| **Content-addressed storage** | SHA-256 keys enable cache validation and deduplication without timestamps |
| **Pydantic v2 models** | Field-level validation, JSON schema export, fast serialization |
| **Ports & Adapters** | Domain code has zero infrastructure imports; swappable backends |
| **Thompson sampling** | Balances exploitation of known-good strategies with exploration of new ones |

## License

MIT License — see [LICENSE](LICENSE) for details.

Dependencies are audited via `pip-licenses`. Run `pip-licenses --format=markdown > docs/LICENSE_AUDIT.md` to regenerate the audit. Note that some optional dependencies use GPL/LGPL licenses; the core Spacescraper pipeline depends only on MIT/BSD/Apache-2.0 licensed packages.

---

<p align="center">
  <sub>Built with the Ports &amp; Adapters architecture · Designed for horizontal scaling · Operable with zero configuration</sub>
</p>
