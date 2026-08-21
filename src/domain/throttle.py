# A3: AutoThrottle as a pure per-domain delay controller.
#
# DomainRateLimiter (src/infrastructure/rate_limiter.py) caps concurrency;
# it has no notion of delay, backoff, or Retry-After. This module is the
# missing controller: given the previous delay and one observation (latency,
# whether the request was blocked, and any server-provided Retry-After), it
# computes the next delay. State lives on DomainProfile.throttle_delay_ms so
# it is shared cluster-wide rather than re-learned per worker.

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def parse_retry_after(value: str) -> float | None:
    """Parse an HTTP Retry-After header (RFC 9110): delay-seconds or an
    HTTP-date. Returns seconds, clamped to >= 0. None for anything malformed
    — callers should treat that the same as "no Retry-After sent"."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max((dt - datetime.now(UTC)).total_seconds(), 0.0)


def compute_next_delay(
    *,
    current_delay_ms: float,
    floor_ms: float,
    latency_ms: float,
    target_concurrency: int,
    ok: bool,
    retry_after_s: float | None,
    max_delay_ms: float,
) -> float:
    """One AutoThrottle step. `current_delay_ms` is the domain's delay going
    into this observation; the return value is what it becomes after it.

    A block can never speed the crawler up: the result is always >= both the
    damped-convergence delay and current_delay_ms, so a single 429 raises the
    floor for every request after it, and a later fast response cannot undo
    that — only an explicit reset of DomainProfile.throttle_delay_ms can.
    """
    target = latency_ms / target_concurrency if target_concurrency > 0 else latency_ms
    delay = max((current_delay_ms + target) / 2, target)

    penalty = 0.0
    if not ok:
        penalty = retry_after_s * 1000 if retry_after_s is not None else current_delay_ms * 2

    delay = max(delay, penalty, current_delay_ms)
    return min(max(delay, floor_ms), max_delay_ms)
