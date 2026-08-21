# Tests for SSRFValidatingTransport (W1.3, finding F13).
#
# ssrf_guard.validate_outbound_url() only checks DNS at job-submit time, in a
# different process than the one that does the actual fetch. This transport
# is what closes that gap: it re-resolves and re-validates at connection
# time, for every request and every redirect hop, and pins the connection to
# the validated IP so nothing can change between the check and the connect.

import httpx
import pytest

import src.security.validating_transport as vt
from src.domain.exceptions import SSRFGuardError
from src.security.validating_transport import SSRFValidatingTransport


def _resolve_to(monkeypatch, ip: str, port: int = 80):
    async def fake_resolve(hostname, requested_port):
        return [(None, None, None, None, (ip, requested_port))]
    monkeypatch.setattr(vt, "_resolve_host", fake_resolve)


@pytest.fixture
def fake_upstream(monkeypatch):
    """Stands in for the real TCP connection — records what reached super()."""
    calls = []

    async def fake_handle(self, request):
        calls.append(request)
        return httpx.Response(200, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_handle)
    return calls


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Isolate tests from the host's real SSRF_EGRESS_ENFORCE — unset now
    means enforce (SEC-1b), same as SSRF_EGRESS_ENFORCE=true."""
    monkeypatch.delenv("SSRF_EGRESS_ENFORCE", raising=False)


@pytest.mark.asyncio
async def test_public_ip_is_pinned_and_forwarded(monkeypatch, fake_upstream):
    _resolve_to(monkeypatch, "93.184.216.34")
    transport = SSRFValidatingTransport()
    request = httpx.Request("GET", "http://example.com/page")

    response = await transport.handle_async_request(request)

    assert response.status_code == 200
    assert len(fake_upstream) == 1
    forwarded = fake_upstream[0]
    assert forwarded.url.host == "93.184.216.34", "connection must pin to the resolved+validated IP"
    assert forwarded.headers["host"] == "example.com"
    assert forwarded.extensions["sni_hostname"] == "example.com"


@pytest.mark.asyncio
async def test_private_ip_blocked_in_enforce_mode(monkeypatch, fake_upstream):
    monkeypatch.setenv("SSRF_EGRESS_ENFORCE", "true")
    _resolve_to(monkeypatch, "127.0.0.1")
    transport = SSRFValidatingTransport()
    request = httpx.Request("GET", "http://internal.example.com/admin")

    with pytest.raises(SSRFGuardError) as exc:
        await transport.handle_async_request(request)

    assert exc.value.code == "SSRF_BLOCKED"
    assert fake_upstream == [], "must not reach the network once blocked"


@pytest.mark.asyncio
async def test_private_ip_blocked_by_default_when_unset(monkeypatch, fake_upstream):
    """SEC-1b: an unset SSRF_EGRESS_ENFORCE must enforce, not silently log-only."""
    _resolve_to(monkeypatch, "169.254.169.254")
    transport = SSRFValidatingTransport()
    request = httpx.Request("GET", "http://sneaky.example.com/steal")

    with pytest.raises(SSRFGuardError) as exc:
        await transport.handle_async_request(request)

    assert exc.value.code == "SSRF_BLOCKED"
    assert fake_upstream == [], "default (unset) mode must block, not just log"


@pytest.mark.asyncio
async def test_private_ip_only_logged_when_explicitly_disabled(monkeypatch, fake_upstream):
    monkeypatch.setenv("SSRF_EGRESS_ENFORCE", "false")
    _resolve_to(monkeypatch, "169.254.169.254")
    transport = SSRFValidatingTransport()
    request = httpx.Request("GET", "http://sneaky.example.com/steal")

    response = await transport.handle_async_request(request)

    assert response.status_code == 200, "explicitly disabled must not block the request"
    assert len(fake_upstream) == 1
    assert fake_upstream[0].url.host == "sneaky.example.com", "log-only path must not rewrite/pin"


@pytest.mark.asyncio
async def test_metadata_hostname_blocked_by_name(monkeypatch, fake_upstream):
    monkeypatch.setenv("SSRF_EGRESS_ENFORCE", "true")
    transport = SSRFValidatingTransport()
    request = httpx.Request("GET", "http://metadata.google.internal/computeMetadata/v1/")

    with pytest.raises(SSRFGuardError):
        await transport.handle_async_request(request)

    assert fake_upstream == []


@pytest.mark.asyncio
async def test_redirect_hop_is_revalidated(monkeypatch, fake_upstream):
    """
    httpx invokes the transport once per redirect hop when follow_redirects=True.
    A URL that resolves to a public IP but 302s to a private one must be
    blocked on the second hop, not waved through because the first hop passed.
    """
    monkeypatch.setenv("SSRF_EGRESS_ENFORCE", "true")
    transport = SSRFValidatingTransport()

    _resolve_to(monkeypatch, "93.184.216.34")
    first_hop = httpx.Request("GET", "http://public.example.com/redirector")
    response = await transport.handle_async_request(first_hop)
    assert response.status_code == 200

    _resolve_to(monkeypatch, "169.254.169.254")
    second_hop = httpx.Request("GET", "http://public.example.com/redirector/target")
    with pytest.raises(SSRFGuardError):
        await transport.handle_async_request(second_hop)


@pytest.mark.asyncio
async def test_unresolvable_hostname_raises_regardless_of_enforce_mode(monkeypatch, fake_upstream):
    import socket

    async def fake_resolve(hostname, port):
        raise socket.gaierror("nope")

    monkeypatch.setattr(vt, "_resolve_host", fake_resolve)
    transport = SSRFValidatingTransport()
    request = httpx.Request("GET", "http://this-does-not-exist.invalid/x")

    with pytest.raises(SSRFGuardError):
        await transport.handle_async_request(request)
    assert fake_upstream == []
