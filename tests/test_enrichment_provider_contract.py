"""
Task 2.2 — Contract tests for the widened EnrichmentProvider port.
Run against every adapter, including NoOp, so the port is real rather than decorative.
"""

import pytest

from src.infrastructure.providers.enrichment_provider import (
    EnrichmentProvider,
    NoOpEnrichmentProvider,
    GeminiEnrichmentProvider,
    LocalLLMProvider,
)
from src.infrastructure.ai.client import AIOrchestrator


ADAPTERS = [
    NoOpEnrichmentProvider(),
    GeminiEnrichmentProvider(),  # no api_key => disabled adapter
    AIOrchestrator(),  # no api_key => disabled orchestrator
    LocalLLMProvider(),  # no base_url/model => disabled adapter
]


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: type(a).__name__)
def test_adapter_satisfies_port(adapter):
    assert isinstance(adapter, EnrichmentProvider)


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: type(a).__name__)
@pytest.mark.asyncio
async def test_is_available_returns_bool(adapter):
    result = await adapter.is_available()
    assert isinstance(result, bool)


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: type(a).__name__)
@pytest.mark.asyncio
async def test_generate_does_not_raise_when_disabled(adapter):
    # All adapters here are unconfigured (no API key) or NoOp; generate must
    # degrade gracefully to None, never raise.
    result = await adapter.generate("test prompt")
    assert result is None


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: type(a).__name__)
@pytest.mark.asyncio
async def test_embed_does_not_raise_when_disabled(adapter):
    result = await adapter.embed("test text")
    assert result is None


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: type(a).__name__)
@pytest.mark.asyncio
async def test_generate_overlay_does_not_raise_when_disabled(adapter):
    result = await adapter.generate_overlay("<html></html>")
    assert result is None


@pytest.mark.asyncio
async def test_noop_enrich_returns_data_unchanged():
    """NoOp is the dark-safe default: enrich is the identity function."""
    provider = NoOpEnrichmentProvider()
    data = {"title": "Widget", "price": 9.99}
    result = await provider.enrich(data)
    assert result == data


@pytest.mark.asyncio
async def test_disabled_gemini_enrich_returns_data_unchanged():
    provider = GeminiEnrichmentProvider()  # no api_key
    data = {"title": "Widget"}
    result = await provider.enrich(data)
    assert result == data


@pytest.mark.asyncio
async def test_ai_orchestrator_is_available_reflects_circuit_state():
    orchestrator = AIOrchestrator(api_key=None)
    assert await orchestrator.is_available() is False

    orchestrator2 = AIOrchestrator(api_key="fake-key-for-test")
    assert await orchestrator2.is_available() is True


@pytest.mark.asyncio
async def test_ai_orchestrator_embed_uses_compute_embedding(monkeypatch):
    """embed() must delegate to compute_embedding (not the removed dead cache)."""
    orchestrator = AIOrchestrator(api_key=None)

    called = {}

    async def fake_compute_embedding(text):
        called["text"] = text
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(orchestrator, "compute_embedding", fake_compute_embedding)

    result = await orchestrator.embed("hello world")
    assert result == [0.1, 0.2, 0.3]
    assert called["text"] == "hello world"


def test_dead_cached_embedding_method_removed():
    """B12: _compute_embedding_cached always returned None and leaked self into
    the cache key. It must be gone, not merely unused."""
    assert not hasattr(AIOrchestrator, "_compute_embedding_cached")
