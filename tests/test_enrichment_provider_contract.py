"""
Task 2.2 — Contract tests for the widened EnrichmentProvider port.
Run against every adapter, including NoOp, so the port is real rather than decorative.
"""

import pytest

from src.infrastructure.ai.openrouter import OpenRouterOrchestrator
from src.infrastructure.providers.enrichment_provider import (
    EnrichmentProvider,
    LocalLLMProvider,
    NoOpEnrichmentProvider,
)

ADAPTERS = [
    NoOpEnrichmentProvider(),
    OpenRouterOrchestrator(),  # no api_key => disabled orchestrator
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
async def test_openrouter_is_available_reflects_circuit_state():
    """No ambient-key fallback: an adapter built without a key stays disabled."""
    assert await OpenRouterOrchestrator(api_key=None).is_available() is False
    assert await OpenRouterOrchestrator(api_key="fake-key-for-test").is_available() is True


@pytest.mark.asyncio
async def test_every_adapter_returns_none_for_embeddings_except_local():
    """OpenRouter serves no embedding models; only the local endpoint can embed."""
    assert await OpenRouterOrchestrator(api_key="k").embed("text") is None
    assert await NoOpEnrichmentProvider().embed("text") is None
