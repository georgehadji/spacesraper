# Regression tests for AI token-cost defects:
#   - AICache L2 wiring and write-through
#   - generate_overlay cache write (was read-only -> permanent 100% miss)
#   - overlay cache key must match the text actually sent to the model
#   - embedding path must call the API and populate its cache
#   - client module must import hashlib / redact_pii

import asyncio

import httpx
import pytest

from src.infrastructure.cache import AICache


class FakeValkey:
    """Minimal async stand-in for the Valkey client used as AICache L2."""

    def __init__(self):
        self.store = {}
        self.setex_calls = 0

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.setex_calls += 1
        self.store[key] = value


@pytest.mark.asyncio
async def test_cache_roundtrip_local():
    cache = AICache(local_maxsize=10, use_valkey=False)
    assert await cache.get("gemini", "overlay", "html") is None
    await cache.set("gemini", "overlay", "html", {"container": ".x"})
    assert await cache.get("gemini", "overlay", "html") == {"container": ".x"}


@pytest.mark.asyncio
async def test_cache_writes_through_to_l2():
    fake = FakeValkey()
    cache = AICache(local_maxsize=10, valkey_client=fake)
    await cache.set("gemini", "overlay", "html", {"a": 1})
    assert fake.setex_calls == 1, "set() must write through to Valkey"


@pytest.mark.asyncio
async def test_cache_reads_from_l2_and_promotes_to_local():
    fake = FakeValkey()
    cache = AICache(local_maxsize=10, valkey_client=fake)
    await cache.set("gemini", "overlay", "html", {"a": 1})
    cache.local.clear()  # simulate a fresh worker process

    assert await cache.get("gemini", "overlay", "html") == {"a": 1}
    assert cache.local_size == 1, "an L2 hit must be promoted into L1"


@pytest.mark.asyncio
async def test_distinct_content_does_not_collide():
    cache = AICache(local_maxsize=10, use_valkey=False)
    await cache.set("gemini", "overlay", "page-a", {"id": "a"})
    await cache.set("gemini", "overlay", "page-b", {"id": "b"})
    assert await cache.get("gemini", "overlay", "page-a") == {"id": "a"}
    assert await cache.get("gemini", "overlay", "page-b") == {"id": "b"}


@pytest.mark.asyncio
async def test_no_valkey_falls_back_to_local_only():
    """use_valkey=False must not attempt a connection or raise."""
    cache = AICache(local_maxsize=10, use_valkey=False)
    await cache.set("gemini", "overlay", "html", {"a": 1})
    assert await cache.get("gemini", "overlay", "html") == {"a": 1}
    assert await cache._get_valkey() is None


# --- AIOrchestrator ---------------------------------------------------------


def _make_orchestrator():
    from src.infrastructure.ai.client import AIOrchestrator

    orch = AIOrchestrator(api_key="test-key")
    orch.cache = AICache(local_maxsize=10, use_valkey=False)
    return orch


def test_client_module_imports_required_symbols():
    """hashlib and redact_pii were used but never imported (NameError at runtime)."""
    from src.infrastructure.ai import client

    assert "hashlib" in client.__dict__
    assert "redact_pii" in client.__dict__


@pytest.mark.asyncio
async def test_generate_overlay_caches_result(monkeypatch):
    orch = _make_orchestrator()
    calls = []
    overlay = {"entity_type": "Opportunity", "container_selector": ".row", "field_mappings": {}}

    async def fake_api(prompt, timeout, is_embedding=False):
        calls.append(prompt)
        return {"candidates": [{"content": {"parts": [{"text": '{"entity_type":"Opportunity","container_selector":".row","field_mappings":{}}'}]}}]}

    monkeypatch.setattr(orch, "_call_gemini_api", fake_api)

    html = "<html><body><div class='row'>x</div></body></html>"
    assert await orch.generate_overlay(html) == overlay
    assert await orch.generate_overlay(html) == overlay
    assert len(calls) == 1, "second identical request must be served from cache"


@pytest.mark.asyncio
async def test_generate_overlay_cache_key_matches_sent_text(monkeypatch):
    """
    Two pages sharing a long identical prefix but differing before the 6000-char
    cut must not share a cache entry. The old key used [:2000] while the prompt
    used [:6000], so they collided.
    """
    orch = _make_orchestrator()
    responses = [
        '{"container_selector":".a","field_mappings":{}}',
        '{"container_selector":".b","field_mappings":{}}',
    ]
    calls = []

    async def fake_api(prompt, timeout, is_embedding=False):
        calls.append(prompt)
        return {"candidates": [{"content": {"parts": [{"text": responses[len(calls) - 1]}]}}]}

    monkeypatch.setattr(orch, "_call_gemini_api", fake_api)

    shared = "<div>" + ("x" * 2500) + "</div>"
    page_a = shared + "<span class='a'>A</span>"
    page_b = shared + "<span class='b'>B</span>"

    assert (await orch.generate_overlay(page_a))["container_selector"] == ".a"
    assert (await orch.generate_overlay(page_b))["container_selector"] == ".b"
    assert len(calls) == 2, "pages differing after 2000 chars must not collide"


@pytest.mark.asyncio
async def test_generate_overlay_does_not_cache_failure(monkeypatch):
    orch = _make_orchestrator()
    calls = []

    async def fake_api(prompt, timeout, is_embedding=False):
        calls.append(prompt)
        return None

    monkeypatch.setattr(orch, "_call_gemini_api", fake_api)

    assert await orch.generate_overlay("<html></html>") is None
    assert await orch.generate_overlay("<html></html>") is None
    assert len(calls) == 2, "failures must not be cached"


@pytest.mark.asyncio
async def test_compute_embedding_calls_api_then_caches(monkeypatch):
    """compute_embedding used to await a sync helper and never call the API."""
    orch = _make_orchestrator()
    orch._embedding_cache.clear()
    calls = []

    async def fake_api(prompt, timeout, is_embedding=False):
        calls.append(prompt)
        return {"embedding": {"values": [0.1, 0.2, 0.3]}}

    monkeypatch.setattr(orch, "_call_gemini_api", fake_api)

    assert await orch.compute_embedding("hello world") == [0.1, 0.2, 0.3]
    assert await orch.compute_embedding("hello world") == [0.1, 0.2, 0.3]
    assert len(calls) == 1, "identical text must be served from the embedding cache"


@pytest.mark.asyncio
async def test_compute_embedding_empty_text_skips_api(monkeypatch):
    orch = _make_orchestrator()

    async def fail(*a, **k):
        raise AssertionError("empty text must not reach the API")

    monkeypatch.setattr(orch, "_call_gemini_api", fail)
    assert await orch.compute_embedding("") is None


@pytest.mark.asyncio
async def test_call_gemini_api_sends_key_as_header_not_url_query(monkeypatch):
    """SEC-2: the API key must never appear in the URL (access/proxy logs,
    Referer headers) — it travels as the x-goog-api-key header instead."""
    from src.infrastructure.ai.client import internal_http

    orch = _make_orchestrator()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["header"] = request.headers.get("x-goog-api-key")
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    real_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_post(url, json=None, timeout=None, headers=None):
        return await real_client.post(url, json=json, timeout=timeout, headers=headers)

    monkeypatch.setattr(internal_http, "post", fake_post)

    try:
        await orch._call_gemini_api("prompt", timeout=1.0)
    finally:
        await real_client.aclose()

    assert "key=" not in seen["url"]
    assert seen["header"] == orch.api_key


@pytest.mark.asyncio
async def test_call_gemini_api_parses_real_httpx_response(monkeypatch):
    """
    Regression for S7: `data = await response.json()` awaited a plain dict
    (httpx.Response.json() is sync), so every real call raised TypeError,
    retried three times, and returned None. Drives http_client.post through
    an httpx.MockTransport so the response is a genuine httpx.Response —
    nothing here can fake its way past a sync/async mismatch.
    """
    from src.infrastructure.ai.client import internal_http

    orch = _make_orchestrator()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "healed"}]}}]},
        )

    real_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_post(url, json=None, timeout=None, headers=None):
        return await real_client.post(url, json=json, timeout=timeout, headers=headers)

    monkeypatch.setattr(internal_http, "post", fake_post)

    try:
        data = await orch._call_gemini_api("prompt", timeout=1.0)
    finally:
        await real_client.aclose()

    assert data is not None, "_call_gemini_api must not return None for a well-formed 200"
    assert data["candidates"][0]["content"]["parts"][0]["text"] == "healed"


@pytest.mark.asyncio
async def test_call_gemini_api_bounds_concurrency(monkeypatch):
    """
    The circuit breaker only reacts after repeated failures; it does nothing
    to cap a concurrent burst. The semaphore added in W1.5 must (F13-adjacent
    finding W1.5 in docs/plans/2026-08-10-architecture-remediation-to-8.5.md).
    """
    from src.infrastructure.ai.client import internal_http

    orch = _make_orchestrator()
    orch._semaphore = asyncio.Semaphore(2)

    concurrent = 0
    max_concurrent = 0
    lock = asyncio.Lock()

    async def fake_post(url, json=None, timeout=None, headers=None):
        # Returns a real httpx.Response so .json() is exercised with the same
        # sync/async contract the real client hits (S7: a hand-rolled fake
        # with `async def json()` masked a broken `await response.json()`).
        nonlocal concurrent, max_concurrent
        async with lock:
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.05)
        async with lock:
            concurrent -= 1
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
        )

    monkeypatch.setattr(internal_http, "post", fake_post)

    results = await asyncio.gather(
        *[orch._call_gemini_api(f"prompt-{i}", timeout=1.0) for i in range(6)]
    )

    assert max_concurrent <= 2, f"semaphore did not bound concurrency: saw {max_concurrent} in flight"
    assert all(r is not None for r in results)
