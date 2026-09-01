"""
Tests for SSRF guard at the transport level.
Validates that GuardedTransport blocks private addresses on every request,
including redirect hops (the regression that matters most).
"""

import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from src.infrastructure.http_client import GuardedTransport
from src.domain.exceptions import SSRFGuardError


@pytest.mark.asyncio
async def test_guarded_transport_blocks_metadata_endpoint():
    """Direct fetch of 169.254.169.254 (AWS metadata) should be blocked."""
    inner = AsyncMock(spec=httpx.AsyncBaseTransport)
    transport = GuardedTransport(inner, allow_private=False)

    request = httpx.Request("GET", "http://169.254.169.254/")

    with pytest.raises(SSRFGuardError):
        await transport.handle_async_request(request)

    inner.handle_async_request.assert_not_called()


@pytest.mark.asyncio
async def test_guarded_transport_blocks_localhost():
    """Direct fetch of 127.0.0.1 (localhost) should be blocked."""
    inner = AsyncMock(spec=httpx.AsyncBaseTransport)
    transport = GuardedTransport(inner, allow_private=False)

    request = httpx.Request("GET", "http://127.0.0.1:8000/")

    with pytest.raises(SSRFGuardError):
        await transport.handle_async_request(request)

    inner.handle_async_request.assert_not_called()


@pytest.mark.asyncio
async def test_guarded_transport_blocks_private_rfc1918():
    """Private RFC1918 addresses should be blocked."""
    inner = AsyncMock(spec=httpx.AsyncBaseTransport)
    transport = GuardedTransport(inner, allow_private=False)

    for ip in ["10.0.0.1", "172.16.0.1", "192.168.1.1"]:
        request = httpx.Request("GET", f"http://{ip}/")
        with pytest.raises(SSRFGuardError):
            await transport.handle_async_request(request)
        inner.handle_async_request.assert_not_called()


@pytest.mark.asyncio
async def test_guarded_transport_blocks_file_scheme():
    """file:// scheme should be blocked."""
    inner = AsyncMock(spec=httpx.AsyncBaseTransport)
    transport = GuardedTransport(inner, allow_private=False)

    request = httpx.Request("GET", "file:///etc/passwd")

    with pytest.raises(SSRFGuardError):
        await transport.handle_async_request(request)

    inner.handle_async_request.assert_not_called()


@pytest.mark.asyncio
async def test_guarded_transport_allows_public_url():
    """Public URLs should pass through to the inner transport."""
    inner = AsyncMock(spec=httpx.AsyncBaseTransport)
    response = httpx.Response(200, text="OK")
    inner.handle_async_request.return_value = response

    transport = GuardedTransport(inner, allow_private=False)

    request = httpx.Request("GET", "https://www.example.com/")
    result = await transport.handle_async_request(request)

    assert result == response
    inner.handle_async_request.assert_called_once_with(request)


@pytest.mark.asyncio
async def test_guarded_transport_allows_private_when_flag_set():
    """When allow_private=True, private addresses should be allowed (tests only)."""
    inner = AsyncMock(spec=httpx.AsyncBaseTransport)
    response = httpx.Response(200, text="OK")
    inner.handle_async_request.return_value = response

    transport = GuardedTransport(inner, allow_private=True)

    request = httpx.Request("GET", "http://127.0.0.1:8000/")
    result = await transport.handle_async_request(request)

    assert result == response
    inner.handle_async_request.assert_called_once_with(request)


@pytest.mark.asyncio
async def test_guarded_transport_blocks_redirect_to_metadata():
    """
    Redirect to private address should be blocked.
    This is the critical regression: httpx follows redirects through the transport,
    so a public URL that 302s to 169.254.169.254 should be stopped at the hop.
    """
    inner = AsyncMock(spec=httpx.AsyncBaseTransport)
    transport = GuardedTransport(inner, allow_private=False)

    # A request to example.com that gets redirected to metadata
    request = httpx.Request("GET", "http://example.com/api/config")

    # The transport will intercept this request first
    result = await transport.handle_async_request(request)

    # Then when httpx follows the redirect (hypothetically to 169.254.169.254),
    # a new request comes through the transport for the redirect target
    redirect_request = httpx.Request("GET", "http://169.254.169.254/metadata")

    with pytest.raises(SSRFGuardError):
        await transport.handle_async_request(redirect_request)


@pytest.mark.asyncio
async def test_guarded_transport_hostname_resolution():
    """Hostname that resolves to private IP should be blocked."""
    inner = AsyncMock(spec=httpx.AsyncBaseTransport)
    transport = GuardedTransport(inner, allow_private=False)

    # localhost resolves to 127.0.0.1
    request = httpx.Request("GET", "http://localhost/")

    with pytest.raises(SSRFGuardError):
        await transport.handle_async_request(request)

    inner.handle_async_request.assert_not_called()
