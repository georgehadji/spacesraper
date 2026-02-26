# Security Hardening & Error Handling Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:writing-plans to create the implementation plan from this design.

**Goal:** Close 8 security vulnerabilities (CRITICAL–HIGH–MEDIUM) and introduce a production-grade error handling system with structured logging, correlation IDs, and sanitized user responses — without disrupting the existing 3-tier worker architecture.

**Architecture:** A layered security middleware chain sits in front of all FastAPI endpoints. A centralized error handler intercepts all exceptions and routes them to either a safe user response or a detailed developer log. A `src/security/` module provides reusable guards and sanitizers. The existing `logger_config.py` is upgraded to emit JSON in production with PII filtering.

**Tech Stack:** FastAPI middleware, Pydantic `EmailStr`, Python `ipaddress` stdlib, Python `logging.Filter`, `python-json-logger`

**Deployment Target:** Docker / Docker Compose (single-host)

---

## Vulnerability Inventory

| ID | Severity | File | Description |
|----|----------|------|-------------|
| C1 | CRITICAL | `src/auth_middleware.py:132` | `validate_key()` accepts any `ss_`-prefixed string — no DB lookup |
| C2 | CRITICAL | `main.py:200` | `/auth/register` unauthenticated, any tier including `enterprise` |
| C3 | CRITICAL | `main.py:120` + `plugins.py:26` | SSRF via `webhook_url` — no internal IP filtering |
| H1 | HIGH | `main.py:107` | CORS wildcard `*` with `allow_credentials=True` |
| H2 | HIGH | `src/application/llm_enrichment.py:54` | Prompt injection — scraped data unsanitized in LLM prompts |
| H3 | HIGH | `src/infrastructure/logger_config.py` | No PII scrubbing — API keys and emails reach `trace.log` |
| M1 | MEDIUM | `src/auth_middleware.py:163` | Non-atomic rate limiting (GET then INCR race condition) |
| M2 | MEDIUM | `main.py:243` | No input size limit on `html_sample` — DoS vector |

---

## Component Design

### 1. `src/security/` — New Module

#### `src/security/ssrf_guard.py`

Validates any URL before the system makes an outbound HTTP request. Used in:
- `main.py` → `JobSubmission.webhook_url` validation at job submission time
- `src/infrastructure/exports/plugins.py` → `WebhookExportPlugin.deliver()`

**Logic:**
```
1. Parse URL scheme — reject anything that is not http/https
2. In production (ENVIRONMENT=production): reject http, require https
3. Resolve hostname to IP via socket.getaddrinfo()
4. Reject if IP falls in any private/reserved range:
   - 127.0.0.0/8   (loopback)
   - 10.0.0.0/8    (RFC1918 private)
   - 172.16.0.0/12 (RFC1918 private)
   - 192.168.0.0/16 (RFC1918 private)
   - 169.254.0.0/16 (link-local / AWS metadata)
   - ::1/128        (IPv6 loopback)
5. Raise SSRFGuardError(code="SSRF_BLOCKED") on violation
```

**API:**
```python
class SSRFGuardError(SpacescraperError):
    pass

def validate_outbound_url(url: str, *, require_https: bool = False) -> None:
    """Raises SSRFGuardError if url targets a private/reserved address."""
```

#### `src/security/input_sanitizer.py`

**Functions:**
```python
def sanitize_for_log(text: str) -> str:
    """
    Masks sensitive patterns before they reach log records.
    Patterns redacted:
    - API keys:  ss_[a-zA-Z0-9_-]{10,} → ss_[REDACTED]
    - Emails:    user@domain.tld → [email redacted]
    - Auth headers: Bearer <token> → Bearer [REDACTED]
    - Postgres DSNs with passwords → [dsn redacted]
    """

def sanitize_for_prompt(text: str) -> str:
    """
    Escapes LLM prompt injection vectors.
    Strips: null bytes, excessive whitespace, role-switching patterns
    (e.g. 'Ignore previous instructions', 'System:').
    Truncates to 2000 chars to limit token cost and injection surface.
    """

def validate_payload_size(data: str, max_bytes: int = 512_000) -> None:
    """
    Raises ValueError if len(data.encode()) > max_bytes.
    Used for html_sample in /autograph endpoint (512KB limit).
    """
```

#### `src/security/cors_config.py`

```python
def build_cors_origins() -> list[str]:
    """
    Returns allowed origins from CORS_ALLOWED_ORIGINS env var
    (comma-separated). Falls back to ["http://localhost:3000",
    "http://localhost:8000"] in development.
    Never returns ["*"].
    """
```

---

### 2. `src/infrastructure/error_handler.py` — New

Centralized FastAPI exception handler registered at app startup.

**Exception → Response mapping:**

| Exception Type | HTTP Status | User Message | Log Level |
|----------------|-------------|--------------|-----------|
| `SSRFGuardError` | 400 | "Invalid webhook URL" | WARNING |
| `SpacescraperError` (other) | 422 | error.message (already safe) | WARNING |
| `HTTPException` | as-is | detail (already safe) | INFO |
| `ValueError` (validation) | 400 | "Invalid input" | WARNING |
| `Exception` (unhandled) | 500 | "An internal error occurred. Reference: {request_id}" | ERROR + full traceback |

**User response envelope (all errors):**
```json
{
  "error": {
    "code": "SSRF_BLOCKED",
    "message": "Invalid webhook URL",
    "request_id": "req_a3f9b12c"
  }
}
```

**Developer log record (JSON, production):**
```json
{
  "timestamp": "2026-02-26T14:30:00Z",
  "level": "WARNING",
  "logger": "Spacescraper.API",
  "request_id": "req_a3f9b12c",
  "exception_type": "SSRFGuardError",
  "exception_code": "SSRF_BLOCKED",
  "message": "SSRF attempt blocked on /jobs",
  "path": "/jobs",
  "method": "POST"
}
```

For 500 errors, the log additionally contains `"traceback": "..."` — this field is **never** sent to the user.

---

### 3. `src/infrastructure/middleware/correlation.py` — New

FastAPI `BaseHTTPMiddleware` that:
1. Reads `X-Request-ID` header if present (for client-side tracing)
2. Generates `req_<8 hex chars>` if absent
3. Stores in `request.state.request_id`
4. Sets `X-Request-ID` on the response

This ID flows into every log record emitted during that request via a `logging.Filter` that reads it from a `contextvars.ContextVar`.

---

### 4. `src/infrastructure/logger_config.py` — Upgrade

**Dual-mode logging (no behavioral change in development):**

```
ENVIRONMENT=development → current colored formatter (unchanged)
ENVIRONMENT=production  → JSON formatter via python-json-logger
```

**JSON log record fields (production):**
```
timestamp, level, logger, message,
request_id (from ContextVar, empty string if outside request),
exception_type, traceback (only on ERROR+)
```

**PII Filter (`PIIRedactFilter`):**
- Attached to all handlers
- Runs `sanitize_for_log()` on the `message` field of every `LogRecord`
- Prevents API keys, emails, DSNs from reaching files or stdout

---

### 5. Auth Fixes (`src/auth_middleware.py`)

**Fix C1 — `validate_key()`:**
- Remove the `ss_` prefix shortcut entirely
- In the interim (before a real DB is added): maintain a small in-memory `dict` of pre-seeded valid keys loaded from the `VALID_API_KEYS` env var (comma-separated)
- Any key not in that dict → `return None`

**Fix C2 — `/auth/register`:**
- Add `EmailStr` type to `AuthRegisterRequest.email` (Pydantic validates format)
- Add a per-IP rate limit: max 5 registrations per IP per day (reuse existing Redis rate limit pattern)
- Restrict self-service to `free` and `basic` tiers only; `pro`/`enterprise` raise 403

**Fix M1 — Atomic rate limiting:**
- Replace GET + INCR pipeline with a Lua script (same pattern as `get_allowed_fanout`)
- Atomically checks current count and increments in one round-trip

---

### 6. `main.py` — Targeted Updates

- Register `CorrelationIDMiddleware` (first in chain)
- Replace `CORSMiddleware` config with `build_cors_origins()`
- Register `RequestSizeLimitMiddleware` (1MB global limit; `/autograph` enforces 512KB via `validate_payload_size`)
- Register global exception handlers from `error_handler.py`
- Add `validate_outbound_url(str(submission.webhook_url))` before job enqueue in `POST /jobs`

---

## What Is NOT Changing (YAGNI)

- SQLite → PostgreSQL migration (separate project)
- OpenTelemetry / Sentry integration (overkill for Docker single-host)
- Full auth system refactor with real database (deferred)
- Worker files (scraper, processor, reporter) — no changes needed
- Redis queue logic — no changes needed

---

## Testing Strategy

Each new component gets a dedicated test file:

| File | What it covers |
|------|----------------|
| `tests/test_security_ssrf_guard.py` | private IPs blocked, public IPs allowed, IPv6 loopback blocked |
| `tests/test_security_input_sanitizer.py` | PII masking, prompt injection stripping, size limit enforcement |
| `tests/test_error_handler.py` | each exception type → correct status + sanitized body + log |
| `tests/test_correlation_middleware.py` | ID generated, ID propagated in response header |
| `tests/test_auth_fixes.py` | ss_ prefix rejected, tier restriction on register, atomic rate limit |

All tests use `pytest` + `httpx.AsyncClient` for in-process API testing (no live Redis required).

---

## File Manifest

**New files:**
- `src/security/__init__.py`
- `src/security/ssrf_guard.py`
- `src/security/input_sanitizer.py`
- `src/security/cors_config.py`
- `src/infrastructure/error_handler.py`
- `src/infrastructure/middleware/__init__.py`
- `src/infrastructure/middleware/correlation.py`
- `tests/test_security_ssrf_guard.py`
- `tests/test_security_input_sanitizer.py`
- `tests/test_error_handler.py`
- `tests/test_correlation_middleware.py`
- `tests/test_auth_fixes.py`

**Modified files:**
- `src/auth_middleware.py` — fix C1 (validate_key), fix C2 (register restrictions), fix M1 (atomic rate limit)
- `src/infrastructure/logger_config.py` — JSON mode + PII filter
- `src/application/llm_enrichment.py` — add `sanitize_for_prompt()` call before prompt construction
- `main.py` — middleware registration, CORS fix, webhook SSRF guard, size limit
- `src/infrastructure/exports/plugins.py` — add `validate_outbound_url()` in `WebhookExportPlugin`
- `src/domain/exceptions.py` — add `SSRFGuardError`, `InputValidationError`
