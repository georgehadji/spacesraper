"""P6 — callers of internal_http that assumed a non-2xx raises (D7).

httpx does not raise on 4xx/5xx and _HttpClient.post does not call
raise_for_status, so a response object comes back for a 401 exactly as it
does for a 200. Two consumers were written as if it did not.

  D7   OpenRouter's _call went straight to response.json(). A 401 body has
       no "choices", so _extract_text returned None, which raised
       ValueError("unparseable response"), which the broad except retried —
       three billed POSTs and ~3s of sleeps per call, with the breaker
       recording "unparseable response" instead of the real cause.

  sibling  WebhookExportPlugin and SlackExportPlugin discard the response
       entirely inside a try/except. A 500 from the webhook was logged as a
       successful dispatch, which also meant worker_reporter's
       DeliveryFailedError could never fire: deliver() had no failure mode
       that reached the gather() above it.

Sweep, as the plan required before choosing where to fix: every
target_http caller (robots.py, sitemap_seeder.py, smart_crawler.py,
worker_scraper.py, cli.py) already branches on status_code explicitly, so
the shared client is not the common cause. Only internal_http's POST
consumers assumed raising — and one consumer, notifier.py, reaches through
get_client() to the raw httpx client, so a raise_for_status flag on the
wrapper would not have covered it either. Fixed at both call sites.
"""

import httpx
import pytest

from src.infrastructure.ai import openrouter as openrouter_mod
from src.infrastructure.ai.ssot import AIJob, profile_for
from src.infrastructure.exports.plugins import SlackExportPlugin, WebhookExportPlugin


def _chat(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}], "model": "test/model"}


def _orchestrator():
    return openrouter_mod.OpenRouterOrchestrator(api_key="test-key")


def _mock_http(monkeypatch, handler) -> dict:
    """Route internal_http.post through a MockTransport, counting requests."""
    counter = {"n": 0}
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_post(url, json=None, timeout=None, headers=None):
        counter["n"] += 1
        return await client.post(url, json=json, timeout=timeout, headers=headers)

    monkeypatch.setattr(openrouter_mod.internal_http, "post", fake_post)
    counter["_client"] = client
    return counter


# --- D7 --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_a_permanent_error_costs_exactly_one_request(monkeypatch):
    """The guard for D7.

    An invalid key is not going to become valid on the second attempt. Every
    retry is a billed POST against a request that cannot succeed.
    """
    calls = _mock_http(
        monkeypatch,
        lambda r: httpx.Response(401, json={"error": {"message": "invalid api key"}}),
    )
    try:
        result = await _orchestrator()._call(profile_for(AIJob.GENERATE), "prompt")
    finally:
        await calls["_client"].aclose()

    assert result is None
    assert calls["n"] == 1, f"a 401 was retried {calls['n']} times"


@pytest.mark.asyncio
async def test_b_the_breaker_records_the_real_cause(monkeypatch, caplog):
    """The status is what the operator has to act on.

    "unparseable response for job=generate" sends them looking at the
    response schema; "OpenRouter 401" sends them to the key.
    """
    calls = _mock_http(
        monkeypatch,
        lambda r: httpx.Response(401, json={"error": {"message": "invalid api key"}}),
    )
    orch = _orchestrator()
    try:
        with caplog.at_level("ERROR"):
            await orch._call(profile_for(AIJob.GENERATE), "prompt")
    finally:
        await calls["_client"].aclose()

    logged = " ".join(caplog.messages)
    assert "401" in logged
    assert "unparseable" not in logged.lower()
    assert orch.failure_count == 1, "a permanent error must still count toward the breaker"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_c_a_retryable_status_is_still_retried(monkeypatch, status):
    """Control: the fix must not turn every non-2xx into a give-up.

    Rate limits and provider outages are exactly what the retry loop is for.
    """
    monkeypatch.setattr(openrouter_mod.asyncio, "sleep", _no_sleep)
    calls = _mock_http(monkeypatch, lambda r: httpx.Response(status, json={}))
    try:
        result = await _orchestrator()._call(profile_for(AIJob.GENERATE), "prompt")
    finally:
        await calls["_client"].aclose()

    assert result is None
    assert calls["n"] == openrouter_mod.RESILIENCE.max_retries


@pytest.mark.asyncio
async def test_d_a_retryable_status_that_recovers_returns_the_answer(monkeypatch):
    """Control: a transient 503 followed by a 200 must still produce text."""
    monkeypatch.setattr(openrouter_mod.asyncio, "sleep", _no_sleep)
    seen = {"n": 0}

    def handler(request):
        seen["n"] += 1
        if seen["n"] == 1:
            return httpx.Response(503, json={})
        return httpx.Response(200, json=_chat("recovered"))

    calls = _mock_http(monkeypatch, handler)
    try:
        result = await _orchestrator()._call(profile_for(AIJob.GENERATE), "prompt")
    finally:
        await calls["_client"].aclose()

    assert result == "recovered"


@pytest.mark.asyncio
async def test_e_a_well_formed_200_is_untouched(monkeypatch):
    """Control: the happy path still costs one request and returns its text."""
    calls = _mock_http(monkeypatch, lambda r: httpx.Response(200, json=_chat("hello")))
    try:
        result = await _orchestrator()._call(profile_for(AIJob.GENERATE), "prompt")
    finally:
        await calls["_client"].aclose()

    assert result == "hello"
    assert calls["n"] == 1


async def _no_sleep(_seconds):
    return None


# --- the export-plugin sibling ---------------------------------------------


class _Recorder:
    def __init__(self, status: int):
        self._status = status
        self.posts = 0

    async def post(self, url, json=None, **kwargs):
        self.posts += 1
        return httpx.Response(self._status, json={}, request=httpx.Request("POST", url))


@pytest.fixture
def record(monkeypatch):
    from src.infrastructure.exports import plugins as plugins_mod

    def _install(status: int) -> _Recorder:
        recorder = _Recorder(status)
        monkeypatch.setattr(plugins_mod.internal_http, "post", recorder.post)
        return recorder

    return _install


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plugin_cls", [WebhookExportPlugin, SlackExportPlugin], ids=["webhook", "slack"]
)
async def test_f_a_rejected_delivery_is_not_reported_as_delivered(
    plugin_cls, record, extracted_record
):
    """The guard for the D7 sibling.

    deliver() swallowed everything, so a 500 from the endpoint read as a
    successful dispatch — and worker_reporter's total-failure detection,
    which gathers these with return_exceptions=True, had nothing to detect.
    """
    record(500)
    plugin = plugin_cls("https://hooks.example.invalid/abc")

    with pytest.raises(Exception) as exc:
        await plugin.deliver([extracted_record])

    assert "500" in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plugin_cls", [WebhookExportPlugin, SlackExportPlugin], ids=["webhook", "slack"]
)
async def test_g_an_accepted_delivery_stays_quiet(plugin_cls, record, extracted_record):
    """Control: a 200 must not raise."""
    recorder = record(200)
    await plugin_cls("https://hooks.example.invalid/abc").deliver([extracted_record])
    assert recorder.posts == 1


@pytest.fixture
def extracted_record():
    from src.domain.models import ExtractedRecord

    return ExtractedRecord(
        record_id="rec_p6",
        record_type="tender",
        data={"title": "Τ", "buyer": "B"},
        source_url="https://example.invalid/a",
    )
