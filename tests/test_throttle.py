# A3: AutoThrottle pure-controller tests.

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

from src.domain.throttle import compute_next_delay, parse_retry_after


def _step(current, latency_ms, ok, retry_after_s=None, target_concurrency=2):
    return compute_next_delay(
        current_delay_ms=current,
        floor_ms=0.0,
        latency_ms=latency_ms,
        target_concurrency=target_concurrency,
        ok=ok,
        retry_after_s=retry_after_s,
        max_delay_ms=60_000.0,
    )


def test_block_with_retry_after_never_drops_below_it_on_the_next_fast_call():
    """The plan's own acceptance test: fast, fast, 429 w/ Retry-After: 30,
    fast — delay must never drop below 30s again."""
    delay = 0.0
    delay = _step(delay, 100, True)
    delay = _step(delay, 100, True)
    delay = _step(delay, 200, False, retry_after_s=30)
    assert delay == 30_000.0
    delay = _step(delay, 100, True)
    assert delay >= 30_000.0, "a block must never speed the crawler up"


def test_block_without_retry_after_doubles_current_delay():
    delay = _step(1000.0, latency_ms=1, ok=False, retry_after_s=None)
    assert delay == 2000.0


def test_max_delay_ceiling_is_respected():
    delay = _step(50_000.0, 100, ok=False, retry_after_s=1_000_000)
    assert delay == 60_000.0


def test_floor_is_respected():
    delay = compute_next_delay(
        current_delay_ms=5.0, floor_ms=500.0, latency_ms=10,
        target_concurrency=2, ok=True, retry_after_s=None, max_delay_ms=60_000.0,
    )
    assert delay == 500.0


def test_delay_never_decreases_within_a_domains_lifetime():
    delay = _step(2000.0, latency_ms=1, ok=True)
    assert delay >= 2000.0


class TestParseRetryAfter:
    def test_integer_seconds(self):
        assert parse_retry_after("30") == 30.0

    def test_negative_seconds_clamped_to_zero(self):
        assert parse_retry_after("-5") == 0.0

    def test_http_date_form(self):
        future = datetime.now(UTC) + timedelta(seconds=120)
        header = format_datetime(future, usegmt=True)
        result = parse_retry_after(header)
        assert result is not None
        assert 90 < result <= 121

    def test_malformed_value_returns_none(self):
        assert parse_retry_after("not-a-date-or-number") is None

    def test_empty_and_none_return_none(self):
        assert parse_retry_after("") is None
        assert parse_retry_after(None) is None
