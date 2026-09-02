# src/security/input_sanitizer.py
import re
from typing import Any

# Patterns that match sensitive data in log messages
_API_KEY_RE = re.compile(r'ss_[a-zA-Z0-9_\-]{10,}')
_EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
_BEARER_RE = re.compile(r'(Bearer\s+)\S+', re.IGNORECASE)
_POSTGRES_DSN_RE = re.compile(r'(postgresql\+?[a-z]*://)[^@]+@')
# Credential-bearing query parameters (SEC-2) — ?key=..., ?token=...,
# ?api_key=..., regardless of casing or separator style.
_QUERY_PARAM_RE = re.compile(r'\b(key|token|api[_-]?key)=[^&\s"\']+', re.IGNORECASE)

# PII field name patterns for redaction before AI API calls
_PII_FIELD_PATTERNS = [
    re.compile(r'phone|telephone|mobile|cell|fax', re.IGNORECASE),
    re.compile(r'email|e-mail', re.IGNORECASE),
    re.compile(r'ssn|social.security|passport|national.id', re.IGNORECASE),
    re.compile(r'credit.card|cc.?num|card.number|pan|cvv|cvc', re.IGNORECASE),
    re.compile(r'password|secret|token|api.?key|auth.?key', re.IGNORECASE),
    re.compile(r'address|street|city|zip|postal.code|state|province', re.IGNORECASE),
    re.compile(r'birth|dob|date.of.birth', re.IGNORECASE),
    re.compile(r'bank.?account|routing|iban|swift|bic', re.IGNORECASE),
]

# Patterns that indicate LLM prompt injection attempts
_INJECTION_PATTERNS = [
    re.compile(r'ignore\s+previous\s+instructions?', re.IGNORECASE),
    re.compile(r'(?<!\w)system\s*:', re.IGNORECASE),
    re.compile(r'<\s*/?system\s*>', re.IGNORECASE),
    re.compile(r'you\s+are\s+now\b', re.IGNORECASE),
    re.compile(r'new\s+instructions?\s*:', re.IGNORECASE),
    re.compile(r'disregard\s+(all\s+)?previous', re.IGNORECASE),
]

def sanitize_for_log(text: Any) -> Any:
    """
    Masks sensitive patterns in a string before it reaches log handlers.

    Redacts: API keys (ss_...), emails, Bearer tokens, PostgreSQL DSNs.
    Returns the input unchanged if it is not a string.
    """
    if not isinstance(text, str):
        return text
    text = _BEARER_RE.sub(r'\1[REDACTED]', text)    # Must be first — consumes full token before API key regex fires
    text = _API_KEY_RE.sub('ss_[REDACTED]', text)
    text = _EMAIL_RE.sub('[email redacted]', text)
    text = _POSTGRES_DSN_RE.sub(r'\1[dsn redacted]@', text)
    text = _QUERY_PARAM_RE.sub(lambda m: f'{m.group(1)}=[REDACTED]', text)
    return text


def sanitize_for_prompt(text: str) -> str:
    """
    Cleans scraped text before it is interpolated into an LLM prompt.

    Strips null bytes and removes prompt injection patterns. Does NOT
    truncate — callers are responsible for their own size budget:
    validate_payload_size() for the raw-input ceiling, and a
    caller-specific compactor (e.g. compact_html_for_prompt) for the
    actual prompt token budget. A previous version truncated to 2000
    chars here, which silently capped every downstream budget at 2000
    regardless of what callers asked for — see F15 in
    docs/plans/2026-08-10-architecture-remediation-to-8.5.md.
    """
    if not isinstance(text, str):
        return ''
    text = text.replace('\x00', '')
    for pattern in _INJECTION_PATTERNS:
        text = pattern.sub('[filtered]', text)
    return text


def validate_payload_size(data: str, max_bytes: int = 512_000) -> None:
    """
    Raises ValueError if the UTF-8 byte length of `data` exceeds `max_bytes`.

    Default limit: 512 KB (suitable for HTML snippets sent to /autograph).
    """
    if not isinstance(data, str):
        raise TypeError(f"validate_payload_size expects str, got {type(data).__name__}")
    size = len(data.encode('utf-8'))
    if size > max_bytes:
        raise ValueError(f"Payload too large: {size} bytes (max {max_bytes})")


def redact_pii(data: dict) -> dict:
    """
    Redact PII fields from a data dict before sending to external AI APIs.
    Replaces values of fields matching PII patterns with '[REDACTED]'.
    """
    redacted = {}
    for key, value in data.items():
        is_pii = any(p.search(str(key)) for p in _PII_FIELD_PATTERNS)
        if is_pii and value is not None:
            redacted[key] = "[REDACTED]"
        elif isinstance(value, dict):
            redacted[key] = redact_pii(value)
        elif isinstance(value, list):
            redacted[key] = [
                redact_pii(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            redacted[key] = value
    return redacted
