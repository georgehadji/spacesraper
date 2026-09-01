"""
Task 4.1 — Tests for LocalLLMProvider and the scoped-client SSRF exemption.

Security-critical: the private-address exemption for a local LLM endpoint
must apply ONLY to that exact host, never as a general relaxation of the
guard, and must never touch the shared HttpClient singleton.
"""

import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from src.infrastructure.http_client import GuardedTransport, create_scoped_client
from src.infrastructure.providers.enrichment_provider import (
    EnrichmentProvider,
    LocalLLMProvider,
)
from src.domain.exceptions import SSRFGuardError


class TestScopedPrivateHostExemption:
    """GuardedTransport.allowed_private_hosts — the mechanism Task 4.1 depends on."""

    @pytest.mark.asyncio
    async def test_exempted_host_bypasses_private_ip_check(self):
        inner = AsyncMock(spec=httpx.AsyncBaseTransport)
        response = httpx.Response(200, text="OK")
        inner.handle_async_request.return_value = response

        transport = GuardedTransport(inner, allowed_private_hosts=frozenset({"localhost"}))
        request = httpx.Request("POST", "http://localhost:11434/v1/chat/completions")

        result = await transport.handle_async_request(request)

        assert result == response
        inner.handle_async_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_other_private_hosts_still_blocked(self):
        """The exemption must not generalize to any other private address."""
        inner = AsyncMock(spec=httpx.AsyncBaseTransport)
        transport = GuardedTransport(inner, allowed_private_hosts=frozenset({"localhost"}))

        # A different private host (e.g. cloud metadata) must still be blocked
        request = httpx.Request("GET", "http://169.254.169.254/metadata")

        with pytest.raises(SSRFGuardError):
            await transport.handle_async_request(request)

        inner.handle_async_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_exemption_by_default(self):
        """Without allowed_private_hosts, behavior is unchanged (fully guarded)."""
        inner = AsyncMock(spec=httpx.AsyncBaseTransport)
        transport = GuardedTransport(inner)

        request = httpx.Request("GET", "http://127.0.0.1:11434/")

        with pytest.raises(SSRFGuardError):
            await transport.handle_async_request(request)

    def test_create_scoped_client_is_not_the_singleton(self):
        """Each call must return an independent client, never the shared singleton."""
        client1 = create_scoped_client(allowed_private_hosts={"localhost"})
        client2 = create_scoped_client(allowed_private_hosts={"localhost"})
        assert client1 is not client2


class TestLocalLLMProvider:
    def test_satisfies_enrichment_provider_port(self):
        provider = LocalLLMProvider(base_url="http://localhost:11434/v1", model="llama3")
        assert isinstance(provider, EnrichmentProvider)

    @pytest.mark.asyncio
    async def test_disabled_without_base_url(self):
        provider = LocalLLMProvider()
        assert await provider.is_available() is False
        assert await provider.generate("hi") is None
        assert await provider.embed("hi") is None
        assert await provider.enrich({"a": 1}) == {"a": 1}  # identity when disabled

    @pytest.mark.asyncio
    async def test_disabled_without_model(self):
        provider = LocalLLMProvider(base_url="http://localhost:11434/v1")
        assert await provider.is_available() is False

    @pytest.mark.asyncio
    async def test_enabled_when_configured(self):
        provider = LocalLLMProvider(base_url="http://localhost:11434/v1", model="llama3")
        assert await provider.is_available() is True

    @pytest.mark.asyncio
    async def test_generate_parses_openai_compatible_response(self):
        provider = LocalLLMProvider(base_url="http://localhost:11434/v1", model="llama3")

        mock_response = MagicMock()
        mock_response.json = MagicMock(return_value={
            "choices": [{"message": {"content": "generated text"}}]
        })
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.generate("test prompt")

        assert result == "generated text"
        # response.json() must be called synchronously (httpx.Response.json is sync,
        # unlike aiohttp) — MagicMock (not AsyncMock) on .json enforces that shape.
        mock_response.json.assert_called_once()

    @pytest.mark.asyncio
    async def test_embed_parses_openai_compatible_response(self):
        provider = LocalLLMProvider(base_url="http://localhost:11434/v1", model="llama3")

        mock_response = MagicMock()
        mock_response.json = MagicMock(return_value={
            "data": [{"embedding": [0.1, 0.2, 0.3]}]
        })
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.embed("some text")

        assert result == [0.1, 0.2, 0.3]

    @pytest.mark.asyncio
    async def test_generate_handles_network_failure_gracefully(self):
        provider = LocalLLMProvider(base_url="http://localhost:11434/v1", model="llama3")

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("connection refused"))

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.generate("test prompt")

        assert result is None

    @pytest.mark.asyncio
    async def test_enrich_parses_json_response(self):
        provider = LocalLLMProvider(base_url="http://localhost:11434/v1", model="llama3")

        mock_response = MagicMock()
        mock_response.json = MagicMock(return_value={
            "choices": [{"message": {"content": '{"title_en": "Widget"}'}}]
        })
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.enrich({"title": "Widget"})

        assert result == {"title_en": "Widget"}

    def test_scoped_client_only_exempts_configured_host(self):
        """The client this provider builds must scope the exemption to its own host."""
        provider = LocalLLMProvider(base_url="http://192.168.1.50:8080/v1", model="llama3")
        assert provider._allowed_host == "192.168.1.50"
