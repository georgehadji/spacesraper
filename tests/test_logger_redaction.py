# Regression test for SEC-2: sanitize_for_log existed but was never wired
# into production logging. A URL with a live-looking API key, logged through
# the actual handler stack (RedactionFilter + CorrelationFilter), must not
# leak the key into the emitted record.

import logging

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
