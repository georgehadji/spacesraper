"""
Task 3.2 — Tests for SearchProvider port and adapters.
Contract tests run against every adapter, including NoOp (the default).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.infrastructure.providers.search_provider import (
    SearchProvider,
    NoOpSearchProvider,
    DuckDuckGoSearchProvider,
    SerperSearchProvider,
)
from src.domain.models import SearchHit


ADAPTERS = [NoOpSearchProvider(), DuckDuckGoSearchProvider(), SerperSearchProvider()]


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: type(a).__name__)
def test_adapter_is_search_provider(adapter):
    assert isinstance(adapter, SearchProvider)


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: type(a).__name__)
@pytest.mark.asyncio
async def test_is_available_returns_bool(adapter):
    result = await adapter.is_available()
    assert isinstance(result, bool)


@pytest.mark.asyncio
async def test_noop_search_returns_empty_list():
    """NoOp is the dark-safe default: no results ever."""
    provider = NoOpSearchProvider()
    hits = await provider.search("anything")
    assert hits == []


@pytest.mark.asyncio
async def test_serper_disabled_without_key_returns_empty():
    provider = SerperSearchProvider(api_key=None)
    assert await provider.is_available() is False
    hits = await provider.search("test query")
    assert hits == []


@pytest.mark.asyncio
async def test_serper_search_parses_organic_results():
    provider = SerperSearchProvider(api_key="fake-key")
    assert await provider.is_available() is True

    # httpx.Response.json() is synchronous — MagicMock (not AsyncMock) on
    # .json enforces that shape, so this test would have caught F4 (search
    # crashed on every real response, silently swallowed) before it shipped.
    mock_response = MagicMock()
    mock_response.json = MagicMock(return_value={
        "organic": [
            {"link": "https://example.com/a", "title": "A", "snippet": "snippet a"},
            {"link": "https://example.com/b", "title": "B", "snippet": "snippet b"},
        ]
    })

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch.object(provider, "_get_client", return_value=mock_client):
        hits = await provider.search("test query", max_results=10)

    assert len(hits) == 2
    assert hits[0].url == "https://example.com/a"
    assert hits[0].provider == "serper"
    assert hits[0].rank == 0
    assert hits[1].rank == 1


@pytest.mark.asyncio
async def test_serper_search_caches_results():
    provider = SerperSearchProvider(api_key="fake-key")

    mock_response = MagicMock()
    mock_response.json = MagicMock(return_value={
        "organic": [{"link": "https://example.com/a", "title": "A", "snippet": "s"}]
    })
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch.object(provider, "_get_client", return_value=mock_client):
        hits1 = await provider.search("cached query")
        hits2 = await provider.search("cached query")

    # Second call should hit cache, not the network
    assert mock_client.post.call_count == 1
    assert hits1 == hits2


@pytest.mark.asyncio
async def test_serper_search_handles_network_failure_gracefully():
    provider = SerperSearchProvider(api_key="fake-key")

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("connection reset"))

    with patch.object(provider, "_get_client", return_value=mock_client):
        hits = await provider.search("test query")

    assert hits == []


@pytest.mark.asyncio
async def test_duckduckgo_parses_html_results():
    provider = DuckDuckGoSearchProvider()

    sample_html = """
    <html><body>
    <div class="result">
        <a class="result__a" href="https://example.com/1">Result One</a>
        <a class="result__snippet">First snippet</a>
    </div>
    <div class="result">
        <a class="result__a" href="https://example.com/2">Result Two</a>
        <a class="result__snippet">Second snippet</a>
    </div>
    </body></html>
    """

    mock_response = MagicMock()
    mock_response.text = sample_html
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch.object(provider, "_get_client", return_value=mock_client):
        hits = await provider.search("test query", max_results=10)

    assert len(hits) == 2
    assert hits[0].url == "https://example.com/1"
    assert hits[0].title == "Result One"
    assert hits[0].provider == "duckduckgo"


@pytest.mark.asyncio
async def test_duckduckgo_handles_network_failure_gracefully():
    provider = DuckDuckGoSearchProvider()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("timeout"))

    with patch.object(provider, "_get_client", return_value=mock_client):
        hits = await provider.search("test query")

    assert hits == []


@pytest.mark.asyncio
async def test_duckduckgo_respects_max_results():
    provider = DuckDuckGoSearchProvider()

    sample_html = "".join(
        f'<div class="result"><a class="result__a" href="https://example.com/{i}">R{i}</a></div>'
        for i in range(20)
    )

    mock_response = MagicMock()
    mock_response.text = sample_html
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch.object(provider, "_get_client", return_value=mock_client):
        hits = await provider.search("test query", max_results=5)

    assert len(hits) == 5


@pytest.mark.asyncio
async def test_serper_search_with_real_httpx_response():
    """
    F4 proof-of-defect / regression guard, using a genuine httpx.Response
    rather than a mock of it — httpx.Response.json() is synchronous, so
    `await response.json()` raises TypeError on a real response, silently
    caught by search()'s broad except and converted to an empty result.
    This must now return real hits, not [].
    """
    import httpx

    provider = SerperSearchProvider(api_key="fake-key")

    real_response = httpx.Response(
        200,
        json={"organic": [{"link": "https://example.com/real", "title": "Real", "snippet": "s"}]},
    )
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=real_response)

    with patch.object(provider, "_get_client", return_value=mock_client):
        hits = await provider.search("real response query")

    assert len(hits) == 1
    assert hits[0].url == "https://example.com/real"
