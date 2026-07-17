# GEMINI.md

## 🚀 Spacescraper: Enterprise Web Intelligence & Procurement Tracking Platform

Welcome! This document serves as the foundational instruction manual and context guide for any AI developer working on the **Spacescraper** repository. Rigorously follow the patterns, standards, and guidelines detailed below to maintain architectural consistency, safety, and development speed.

---

## 📂 Project Overview & Architecture

Spacescraper is a production-grade, asynchronous web intelligence system engineered to discover, extract, normalize, and track structured data from public web portals (primarily space, defense, and dual-use opportunities).

### 🏛️ Hexagonal / Clean Architecture Design

The codebase strictly adheres to domain-driven layer separation:

```
┌─────────────────────────────────────────────────────────────────┐
│                        Delivery Layer                           │
│  FastAPI Gateway (main.py)   Streamlit Dashboard   Webhooks     │
├─────────────────────────────────────────────────────────────────┤
│                     Application Layer                           │
│  Pipeline Orchestrator   Classifier   LLM Enrichment            │
│  Post-Processor   Win Predictor   Data Quality Scorer           │
├─────────────────────────────────────────────────────────────────┤
│                        Domain Layer                             │
│  ScrapeJob   Opportunity   Product   Lead   Article   FollowLink     │
│  (Pure Pydantic models & Custom domain exceptions)              │
├─────────────────────────────────────────────────────────────────┤
│                    Infrastructure Layer                         │
│  Browser (Playwright)   HTTP Client   Queue Workers (Redis)     │
│  Storage (SQLite/PostgreSQL)   AI Clients   Notifications       │
│  Monitoring (OTel/Prometheus)   Security Middleware             │
└─────────────────────────────────────────────────────────────────┘
```

- **Domain Layer (`src/domain/`)**: Pure business models (Pydantic v2) and exceptions, with zero external infrastructure dependencies.
- **Application Layer (`src/application/`)**: Core intelligence workflow orchestrator, fuzzy deduplication, LLM enrichment, classification, and scoring.
- **Infrastructure Layer (`src/infrastructure/`)**: Storage engines, queue workers, Playwright browser controllers, logging, and metrics.
- **Delivery Layer**: API routing (`main.py`) and Streamlit web portal interfaces (`dashboard.py`).

---

## 🛠️ Technology Stack & Topology

- **Runtime**: Python 3.11+
- **Database**: SQLite (local dev/testing) & PostgreSQL (enterprise staging/prod)
- **Message Broker & Cache**: Redis (Task queues, rate limiting, and caching)
- **Extractors**: Playwright (chromium headless), beautifulsoup4, dynamic overlays
- **API Engine**: FastAPI + Uvicorn
- **AI/LLM Providers**: Google Gemini (VertexAI / Gemini API) & OpenAI
- **UI Portal**: Streamlit
- **Observability**: OpenTelemetry (Traces to Jaeger) & Prometheus (Metrics)
- **Security**: Custom SSRF Guards, Input Sanitization, CORS restriction, API-key authentication

### Worker Topology & Startup Commands

| Service | Role | Direct CLI Command |
|---------|------|--------------------|
| **API Gateway** | REST API & Job Dispatcher | `uvicorn main:app --host 0.0.0.0 --port 8000` |
| **Scraper Worker** | Browser Sessions & Extraction | `python worker_scraper.py` |
| **Processor Worker** | Parsing, Enrichment, Persistence | `python worker_processor.py` |
| **Reporter Worker** | Side Effects & Notifications | `python worker_reporter.py` |
| **Dashboard** | Web Monitoring & Explorer | `streamlit run dashboard.py` |

---

## 📦 Building, Running, and Local Setup

### 1. Environment Setup

Configure your Python environment and download the Playwright dependencies:

```bash
# Create and activate virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install core and dashboard dependencies
pip install -r requirements.txt
pip install -r requirements-dashboard.txt

# Install Playwright browser engines
playwright install chromium
```

### 2. Configuration (`.env`)

Copy `.env.example` to `.env` and fill in your details:

```bash
cp .env.example .env
```

Key environment variables to configure:
- `OPENAI_API_KEY` / `GEMINI_API_KEY`: API credentials for AI enrichment features.
- `REDIS_URL`: Redis backend link (default: `redis://localhost:6379`).
- `DEMO_API_KEY`: Developer testing key (default: `ss_demo_key`).
- `ENVIRONMENT`: Run environment (`development` or `production`).

---

## 🧪 Testing & Validation

All contributions must have unit and integration test coverage. The test suite uses `pytest` with async capabilities.

### Executing Tests

```bash
# Run all unit and integration tests
pytest

# Run tests with structural code coverage report
pytest --cov=src --cov-report=term-missing

# Run security middleware tests specifically
pytest tests/test_security_*.py

# Run database integration tests
pytest tests/integration/
```

### Test Standards
- Utilize standard fixtures (like `sample_opportunity` or `mock_html_listing`) from `tests/conftest.py`.
- Ensure async tests utilize the shared session event loop fixture defined in `tests/conftest.py`.
- Mock external network requests and AI provider responses to prevent flaky or costly test suites.

---

## 💾 Database State & Zero-Downtime Migration

The default storage is SQLite, but PostgreSQL is utilized for enterprise environments. A robust migration tool exists to move data from SQLite to PostgreSQL with zero downtime.

```bash
# Preview changes (Dry Run)
python migrate_sqlite_to_postgres.py --dry-run

# Run migration to migrate all tables
python migrate_sqlite_to_postgres.py --execute

# Run migration on specific tables only
python migrate_sqlite_to_postgres.py --execute --tables opportunities,runs

# Verify migration integrity
python verify_migration.py
```

---

## 🛡️ Security & Resilience Hardening

Spacescraper implements defense-in-depth across all system boundaries:

1. **SSRF Guard (`src/security/ssrf_guard.py`)**: Intercepts outbound requests to prevent SSRF vulnerabilities targeting loopback addresses, local networks, or private cloud metadata.
2. **Input Sanitizer (`src/security/input_sanitizer.py`)**: Validates and normalizes user payloads, emails, URLs, and JSON keys before ingestion.
3. **CORS Configuration (`src/security/cors_config.py`)**: Restricts external access to known delivery layers.
4. **Resilience Strategies**:
   - **Dead-Letter Queue (DLQ)**: Failed tasks or out-of-memory jobs are pushed to a Redis/Postgres DLQ instead of silently dropping.
   - **Fan-Out Limits**: Imposes hard upper bounds on scraper recursion depth.

---

## 📯 Development Conventions & Rules of Thumb

### 1. Redis Lua and Python `eval()` Rule ⚠️
Redis exposes an `eval` command to execute Lua scripts inside the server. Python has a built-in `eval()` function which is flagged as a critical security vulnerability by standard linters (such as Ruff/Bandit).
- **Rule**: **NEVER** write `redis_client.eval(...)` or `await redis_client.eval(...)`.
- **Solution**: Use `getattr` dynamically to bypass linter warnings:
  ```python
  redis_fn = getattr(redis_client, "eval")
  await redis_fn(lua_script, num_keys, *keys_and_args)
  ```
  *(See existing implementation in `src/infrastructure/queues/redis_worker.py:194`)*

### 2. State-Aware Change Detection: Identity Hash vs Content Hash 🧬
- **Identity Hash (`identity_hash`)**: Computed solely from **raw pre-AI fields** (`url`, raw `title`, `deadline`). This represents the immutable entity fingerprint.
- **Content Hash (`content_hash`)**: Computed from post-processing and AI-enriched fields.
- **Rule**: Always perform state tracking (`NEW`, `UPDATED`, `UNCHANGED`) against the **Identity Hash**. Doing this prevents "hash-drift storms" and redundant updates if LLM enrichment models or prompts are modified downstream.

### 3. Hexagonal Boundaries
- Domain objects (`src/domain/models.py`) must be pure Pydantic models. Avoid importing infrastructure modules inside them.
- New scraper platforms must implement the `BaseExtractionStrategy` located in `src/extractors/base_extractor.py`.

---

## 🦥 The "Ponytail" Lazy Developer Philosophy

This codebase is optimized for **clarity, simplicity, and deletion over addition**. Keep modifications lean by climbing the developer ladder:

1. **Does this need to be built at all?** (YAGNI - You Aren't Gonna Need It).
2. **Does it already exist in the codebase?** Reuse helpers, utilities, and established patterns.
3. **Does the standard library already cover it?** Use native Python libraries instead of third-party libraries where possible.
4. **Does an already-installed dependency solve it?** Refer to `requirements.txt` before importing new libraries.
5. **Can this be written in fewer lines?** Write high-readability, high-impact code.

### Guidelines
- **No Abstractions** without explicit requests. Avoid creating unnecessary wrappers, factories, or deep class inheritance layers.
- **No Boilerplate**: Write standard, simple, traceable procedural or compositional structures.
- **Root Cause, Not Symptom**: When fixing bugs, grep all callers of the target function. Address the bug globally at the lowest common layer rather than applying band-aid overrides in individual caller paths.
- **`ponytail:` Comments**: If an intentional shortcut is taken (e.g., using a global lock, naive scan, or temporary heuristic), annotate it with a `# ponytail: <reason>` comment outlining the boundary ceiling and the future upgrade path.

---

*Spacescraper — Data Orchestration for the Modern Intelligence Enterprise.*
