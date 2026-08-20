# Regression tests for S3 (endpoint capture, widened content-type match,
# errors are signal not silence) and SEC-3 (bounded interception buffer).

import json

import pytest

import src.infrastructure.browser.engine as engine_module
from src.infrastructure.browser.engine import ScraperEngine


class FakeResponse:
    def __init__(self, url, content_type, body: bytes, status=200, ok=True):
        self.url = url
        self._content_type = content_type
        self._body = body
        self.status = status
        self.ok = ok

    @property
    def headers(self):
        return {"content-type": self._content_type}

    async def body(self):
        return self._body


def _engine() -> ScraperEngine:
    return ScraperEngine(context_pool=None, timeout=1000)


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", [
    "application/json",
    "application/ld+json",
    "text/json; charset=utf-8",
    "application/vnd.api+json",
])
async def test_widened_content_type_match_captures_endpoint(content_type):
    engine = _engine()
    body = json.dumps({"ok": True}).encode()
    await engine._intercept_response(FakeResponse("https://x.test/api", content_type, body))

    assert len(engine.intercepted_json) == 1
    captured = engine.intercepted_json[0]
    assert captured["url"] == "https://x.test/api"
    assert captured["status"] == 200
    assert captured["content_type"] == content_type
    assert captured["data"] == {"ok": True}


@pytest.mark.asyncio
async def test_non_json_content_type_is_not_captured():
    engine = _engine()
    await engine._intercept_response(FakeResponse("https://x.test/page", "text/html", b"<html></html>"))
    assert engine.intercepted_json == []


@pytest.mark.asyncio
async def test_interception_error_is_counted_not_swallowed_silently():
    engine = _engine()
    # Malformed JSON body: json.loads raises, exercising the except path.
    await engine._intercept_response(FakeResponse("https://x.test/api", "application/json", b"{not json"))
    assert engine.intercepted_json == []
    assert engine._interception_errors == 1


@pytest.mark.asyncio
async def test_per_response_size_cap_drops_oversized_body(monkeypatch):
    monkeypatch.setattr(engine_module, "INTERCEPT_MAX_RESPONSE_BYTES", 10)
    engine = _engine()
    body = json.dumps({"data": "x" * 100}).encode()
    await engine._intercept_response(FakeResponse("https://x.test/api", "application/json", body))

    assert engine.intercepted_json == []
    assert engine._intercept_overflow_count == 1


@pytest.mark.asyncio
async def test_per_page_count_cap_stops_further_capture(monkeypatch):
    monkeypatch.setattr(engine_module, "INTERCEPT_MAX_COUNT", 2)
    engine = _engine()
    for i in range(3):
        await engine._intercept_response(
            FakeResponse(f"https://x.test/api/{i}", "application/json", b"{}")
        )

    assert len(engine.intercepted_json) == 2
    assert engine._intercept_overflow_count == 1


@pytest.mark.asyncio
async def test_per_page_total_bytes_cap_stops_further_capture(monkeypatch):
    monkeypatch.setattr(engine_module, "INTERCEPT_MAX_TOTAL_BYTES", 15)
    engine = _engine()
    body = b'{"a":1}'  # 7 bytes
    await engine._intercept_response(FakeResponse("https://x.test/a", "application/json", body))
    await engine._intercept_response(FakeResponse("https://x.test/b", "application/json", body))
    # cumulative 14 bytes so far; this one would push past the 15-byte cap
    await engine._intercept_response(FakeResponse("https://x.test/c", "application/json", body))

    assert len(engine.intercepted_json) == 2
    assert engine._intercept_overflow_count == 1
