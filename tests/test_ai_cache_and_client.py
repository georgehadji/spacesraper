# Regression tests for AI token-cost defects:
#   - AICache L2 wiring and write-through
#   - generate_overlay cache write (was read-only -> permanent 100% miss)
#   - overlay cache key must match the text actually sent to the model
#   - embedding path must call the API and populate its cache
#   - client module must import hashlib / redact_pii

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
    overlay = {"entity_type": "Opportunity", "container": ".row", "mapping": {}}

    async def fake_api(prompt, timeout, is_embedding=False):
        calls.append(prompt)
        return {"candidates": [{"content": {"parts": [{"text": '{"entity_type":"Opportunity","container":".row","mapping":{}}'}]}}]}

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
        '{"container":".a","mapping":{}}',
        '{"container":".b","mapping":{}}',
    ]
    calls = []

    async def fake_api(prompt, timeout, is_embedding=False):
        calls.append(prompt)
        return {"candidates": [{"content": {"parts": [{"text": responses[len(calls) - 1]}]}}]}

    monkeypatch.setattr(orch, "_call_gemini_api", fake_api)

    shared = "<div>" + ("x" * 2500) + "</div>"
    page_a = shared + "<span class='a'>A</span>"
    page_b = shared + "<span class='b'>B</span>"

    assert (await orch.generate_overlay(page_a))["container"] == ".a"
    assert (await orch.generate_overlay(page_b))["container"] == ".b"
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
