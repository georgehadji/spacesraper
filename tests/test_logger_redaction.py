# Regression test for SEC-2: sanitize_for_log existed but was never wired
# into production logging. A URL with a live-looking API key, logged through
# the actual handler stack (RedactionFilter + CorrelationFilter), must not
# leak the key into the emitted record.

import logging
import sys
from pathlib import Path

import pytest

from src.infrastructure.logger_config import CorrelationFilter, RedactionFilter


def _make_logger_with_filters(handler: logging.Handler) -> tuple[logging.Logger, logging.Handler]:
    logger = logging.getLogger("test.sec2.redaction")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False
    handler.addFilter(RedactionFilter())
    handler.addFilter(CorrelationFilter())
    logger.addHandler(handler)
    return logger, handler


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


def test_url_with_api_key_emits_no_key_material_through_logging_stack():
    handler = _ListHandler()
    logger, handler = _make_logger_with_filters(handler)

    logger.warning(
        "SSRF egress guard blocked request to https://generativelanguage.googleapis.com/"
        "v1beta/models/gemini-1.5-flash:generateContent?key=AIzaSyABCDEF1234567890abcdef"
    )

    assert len(handler.records) == 1
    emitted = handler.records[0]
    assert "AIzaSyABCDEF1234567890abcdef" not in emitted
    assert "key=[REDACTED]" in emitted


def test_percent_style_args_do_not_break_redaction():
    handler = _ListHandler()
    logger, handler = _make_logger_with_filters(handler)

    logger.info("request to %s failed with key=%s", "https://x/y", "AIzaLiveLookingKey")

    assert len(handler.records) == 1
    emitted = handler.records[0]
    assert "AIzaLiveLookingKey" not in emitted
    assert "key=[REDACTED]" in emitted


# ---------------------------------------------------------------------------
# The filter working is only half of SEC-2 -- it also has to be installed in
# every process. boot.py starts each worker with create_subprocess_exec, so a
# worker gets its own interpreter and its own root logger; a call in a sibling
# module reaches nothing. worker_scraper.py had no call at all and
# worker_processor.py's had been commented out since the initial commit, which
# left the two processes that handle raw fetched content and target
# credentials logging unredacted.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

ENTRY_POINTS = [
    "main.py",
    "spacescraper.py",
    "worker_scraper.py",
    "worker_processor.py",
    "worker_discovery.py",
    "worker_reporter.py",
]


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_entry_point_installs_production_logging(entry_point):
    source = (REPO_ROOT / entry_point).read_text(encoding="utf-8")
    live_calls = [
        line for line in source.splitlines()
        if line.strip() == "setup_production_logging()"
    ]
    assert live_calls, (
        f"{entry_point} runs as its own process but never calls "
        "setup_production_logging(), so nothing redacts its logs. A commented-out "
        "call does not count -- that is exactly how this regressed."
    )


def test_cli_redacts_without_writing_logs_to_stdout():
    """cli.py cannot use setup_production_logging (that handler targets stdout,
    which is the CLI's JSON channel), so it wires the filter up itself."""
    import cli

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        cli._configure_logging(verbose=True)
        assert any(
            isinstance(f, RedactionFilter) for h in root.handlers for f in h.filters
        ), "--verbose logs whole URLs; a token in a query string would reach stderr in the clear"
        assert not any(
            getattr(h, "stream", None) is sys.stdout for h in root.handlers
        ), "a log handler on stdout corrupts the pure-JSON contract"
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
