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



# --- OpenRouterOrchestrator -------------------------------------------------
# These were originally written against the direct-Gemini AIOrchestrator. That
# adapter is gone (Gemini is reached through OpenRouter now), but the bugs they
# pin are properties of the shared cache/transport code, not of Gemini, so they
# are ported rather than deleted.


def _make_orchestrator():
    from src.infrastructure.ai.openrouter import OpenRouterOrchestrator

    orch = OpenRouterOrchestrator(api_key="test-key")
    orch.cache = AICache(local_maxsize=10, use_valkey=False)
    return orch


def _chat(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


OVERLAY_JSON = '{"entity_type":"Opportunity","container_selector":".row","field_mappings":{}}'


@pytest.mark.asyncio
async def test_generate_overlay_caches_result(monkeypatch):
    """Was read-only once, giving a permanent 100% miss and re-billing every call."""
    orch = _make_orchestrator()
    calls = []

    async def fake_call(profile, prompt):
        calls.append(prompt)
        return OVERLAY_JSON

    monkeypatch.setattr(orch, "_call", fake_call)

    html = "<html><body><div class='row'>x</div></body></html>"
    expected = {"entity_type": "Opportunity", "container_selector": ".row", "field_mappings": {}}
    assert await orch.generate_overlay(html) == expected
    assert await orch.generate_overlay(html) == expected
    assert len(calls) == 1, "second identical request must be served from cache"


@pytest.mark.asyncio
async def test_generate_overlay_cache_key_matches_sent_text(monkeypatch):
    """Two pages sharing a long prefix but differing before the truncation point
    must not share a cache entry. The old key used [:2000] while the prompt used
    more, so different pages collided."""
    orch = _make_orchestrator()
    calls = []

    async def fake_call(profile, prompt):
        calls.append(prompt)
        # Match on the element text, not the attribute: the compactor normalises
        # class='a' to class="a", so a single-quoted marker never matches.
        selector = ".a" if ">A<" in prompt else ".b"
        return (
            '{"entity_type":"Opportunity","container_selector":"'
            + selector
            + '","field_mappings":{}}'
        )

    monkeypatch.setattr(orch, "_call", fake_call)

    shared = "<html><body>" + ("<p>filler</p>" * 400)
    assert (await orch.generate_overlay(shared + "<span class='a'>A</span>"))["container_selector"] == ".a"
    assert (await orch.generate_overlay(shared + "<span class='b'>B</span>"))["container_selector"] == ".b"
    assert len(calls) == 2, "pages differing after the key prefix must not collide"


@pytest.mark.asyncio
async def test_generate_overlay_does_not_cache_failure(monkeypatch):
    orch = _make_orchestrator()
    calls = []

    async def fake_call(profile, prompt):
        calls.append(prompt)
        return None

    monkeypatch.setattr(orch, "_call", fake_call)

    assert await orch.generate_overlay("<html></html>") is None
    assert await orch.generate_overlay("<html></html>") is None
    assert len(calls) == 2, "failures must not be cached"


@pytest.mark.asyncio
async def test_api_key_travels_as_a_header_not_a_url_query(monkeypatch):
    """SEC-2: the key must never appear in the URL (access/proxy logs, Referer)."""
    from src.infrastructure.ai import openrouter as openrouter_mod

    orch = _make_orchestrator()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=_chat("ok"))

    real_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_post(url, json=None, timeout=None, headers=None):
        return await real_client.post(url, json=json, timeout=timeout, headers=headers)

    monkeypatch.setattr(openrouter_mod.internal_http, "post", fake_post)

    from src.infrastructure.ai.ssot import AIJob, profile_for
    try:
        assert await orch._call(profile_for(AIJob.GENERATE), "prompt") == "ok"
    finally:
        await real_client.aclose()

    assert "key=" not in seen["url"]
    assert seen["auth"] == f"Bearer {orch.api_key}"


@pytest.mark.asyncio
async def test_call_parses_a_real_httpx_response(monkeypatch):
    """S7: a hand-rolled fake with `async def json()` once masked a broken
    `await response.json()`, so this exercises the real sync/async contract."""
    from src.infrastructure.ai import openrouter as openrouter_mod
    from src.infrastructure.ai.ssot import AIJob, profile_for

    orch = _make_orchestrator()
    real_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_chat("hello")))
    )

    async def fake_post(url, json=None, timeout=None, headers=None):
        return await real_client.post(url, json=json, timeout=timeout, headers=headers)

    monkeypatch.setattr(openrouter_mod.internal_http, "post", fake_post)
    try:
        text = await orch._call(profile_for(AIJob.GENERATE), "prompt")
    finally:
        await real_client.aclose()

    assert text == "hello", "_call must not return None for a well-formed 200"


@pytest.mark.asyncio
async def test_call_bounds_concurrency(monkeypatch):
    """The circuit breaker only reacts after repeated failures; it does nothing
    to cap a concurrent burst. The semaphore added in W1.5 must."""
    from src.infrastructure.ai import openrouter as openrouter_mod
    from src.infrastructure.ai.ssot import AIJob, profile_for

    orch = _make_orchestrator()
    orch._semaphore = asyncio.Semaphore(2)

    concurrent = 0
    max_concurrent = 0
    lock = asyncio.Lock()

    async def fake_post(url, json=None, timeout=None, headers=None):
        nonlocal concurrent, max_concurrent
        async with lock:
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.05)
        async with lock:
            concurrent -= 1
        return httpx.Response(200, json=_chat("ok"))

    monkeypatch.setattr(openrouter_mod.internal_http, "post", fake_post)

    profile = profile_for(AIJob.GENERATE)
    results = await asyncio.gather(*[orch._call(profile, f"prompt-{i}") for i in range(6)])

    assert max_concurrent <= 2, f"semaphore did not bound concurrency: saw {max_concurrent} in flight"
    assert all(r is not None for r in results)


@pytest.mark.asyncio
async def test_embeddings_are_unavailable():
    """OpenRouter serves no embedding models; dedup falls back to fuzzy matching."""
    orch = _make_orchestrator()
    assert await orch.embed("some text") is None
    assert await orch.compute_embedding("some text") is None
