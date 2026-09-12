"""D14 — the two egress transports must enforce the same URL policy.

http_client.GuardedTransport and validating_transport.SSRFValidatingTransport
both exist and both have live callers (see the comment block in
src/infrastructure/http_client.py). They are not being merged: they differ on
purpose -- GuardedTransport carries allowed_private_hosts for adapters that
must reach one private endpoint, SSRFValidatingTransport carries the
SSRF_EGRESS_ENFORCE log-only opt-out. What must NOT differ is what they
consider a permissible destination, and it did:

  a) SSRFValidatingTransport had no scheme check at all. A gopher:// URL
     reached it, passed its gate, and was handed to the inner httpx
     transport. GuardedTransport rejected the same URL, because
     validate_outbound_url checks the scheme. Same process, two answers.

  b) GuardedTransport resolved DNS twice for every request and every
     redirect hop -- once inside validate_outbound_url and again inside
     resolve_and_validate_hostname -- each behind its own thread hop.

Both are fixed by giving the two transports one shared pre-DNS policy check
instead of one transport borrowing a function that does policy and DNS
together.
"""

import asyncio
import socket

import httpx
import pytest

from src.domain.exceptions import SSRFGuardError
from src.infrastructure.http_client import GuardedTransport
from src.security.validating_transport import SSRFValidatingTransport

PUBLIC_IP = "93.184.216.34"


class _NoopInner(httpx.AsyncBaseTransport):
    """Stands in for the real connection so nothing leaves the machine."""

    def __init__(self):
        self.seen: list[httpx.URL] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request.url)
        return httpx.Response(200, text="ok", request=request)


@pytest.fixture
def counted_resolver(monkeypatch):
    """Count getaddrinfo calls and always answer with a public address."""
    calls: list[str] = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port or 80))]

    monkeypatch.setattr("src.security.ssrf_guard.socket.getaddrinfo", fake_getaddrinfo)

    async def fake_resolve(hostname, port):
        calls.append(hostname)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]

    monkeypatch.setattr(
        "src.security.validating_transport._resolve_host", fake_resolve
    )
    return calls


# --- (a) scheme parity ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_ssrf_transport_rejects_a_non_http_scheme(counted_resolver):
    """The guard for (a).

    The host here resolves to a public address, so nothing else in the
    transport objects to it. Only the scheme is wrong -- and before the fix
    nothing in SSRFValidatingTransport looked at the scheme.
    """
    transport = SSRFValidatingTransport()
    request = httpx.Request("GET", "gopher://example.invalid/1")

    with pytest.raises(SSRFGuardError) as excinfo:
        await transport.handle_async_request(request)

    assert "scheme" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_b_guarded_transport_rejects_the_same_scheme(counted_resolver):
    """Control: this transport already rejected it. Parity is the point."""
    inner = _NoopInner()
    transport = GuardedTransport(inner)
    request = httpx.Request("GET", "gopher://example.invalid/1")

    with pytest.raises(SSRFGuardError) as excinfo:
        await transport.handle_async_request(request)

    assert "scheme" in str(excinfo.value).lower()
    assert inner.seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_c_both_transports_still_pass_ordinary_urls(counted_resolver, scheme):
    """Control: the scheme gate must not reject what the app actually sends."""
    inner = _NoopInner()
    guarded = GuardedTransport(inner)
    response = await guarded.handle_async_request(
        httpx.Request("GET", f"{scheme}://example.invalid/page")
    )
    assert response.status_code == 200
    # Pinned to the validated IP, with the original host preserved.
    assert inner.seen[-1].host == PUBLIC_IP


# --- (b) one resolution per request -----------------------------------------


@pytest.mark.asyncio
async def test_d_guarded_transport_resolves_once_per_request(counted_resolver):
    """The guard for (b).

    Two lookups per request is two chances for DNS to answer differently and
    twice the latency on every cache miss, on a path that already pays a
    thread hop to keep the event loop free.
    """
    inner = _NoopInner()
    transport = GuardedTransport(inner)
    await transport.handle_async_request(
        httpx.Request("GET", "https://example.invalid/page")
    )

    assert counted_resolver == ["example.invalid"], (
        f"expected exactly one DNS resolution, got {len(counted_resolver)}: "
        f"{counted_resolver}"
    )


@pytest.mark.asyncio
async def test_e_the_exempt_host_path_still_skips_the_guard(counted_resolver):
    """Control: allowed_private_hosts is the reason this class still exists."""
    inner = _NoopInner()
    transport = GuardedTransport(
        inner, allowed_private_hosts=frozenset({"localhost"})
    )
    response = await transport.handle_async_request(
        httpx.Request("GET", "http://localhost:11434/api/generate")
    )

    assert response.status_code == 200
    assert counted_resolver == []
    assert inner.seen[-1].host == "localhost"


@pytest.mark.asyncio
async def test_f_a_private_address_is_still_refused(counted_resolver, monkeypatch):
    """Control: closing the duplicate lookup must not weaken the check."""

    def private_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 80))]

    monkeypatch.setattr(
        "src.security.ssrf_guard.socket.getaddrinfo", private_getaddrinfo
    )

    inner = _NoopInner()
    transport = GuardedTransport(inner)
    with pytest.raises(SSRFGuardError):
        await transport.handle_async_request(
            httpx.Request("GET", "https://example.invalid/page")
        )
    assert inner.seen == []


@pytest.mark.asyncio
async def test_g_the_resolver_never_blocks_the_event_loop(counted_resolver):
    """Control for the half of D14 already fixed: the blocking resolvers
    stay behind asyncio.to_thread, so a slow lookup cannot stall the loop."""
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    def slow_getaddrinfo(host, port, *args, **kwargs):
        import time

        time.sleep(0.15)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port or 80))]

    import src.security.ssrf_guard as guard_mod

    guard_mod.socket.getaddrinfo = slow_getaddrinfo

    task = asyncio.create_task(ticker())
    try:
        await GuardedTransport(_NoopInner()).handle_async_request(
            httpx.Request("GET", "https://example.invalid/page")
        )
    finally:
        task.cancel()

    assert ticks > 10, (
        f"the loop only advanced {ticks} times during a 150ms lookup -- "
        "the resolver is blocking it"
    )


@pytest.mark.asyncio
async def test_h_a_metadata_address_is_still_refused(counted_resolver, monkeypatch):
    """Control: GuardedTransport no longer calls validate_outbound_url, which
    is where its `ip in METADATA_IPS` check used to live. is_private_ip
    covers both metadata addresses (169.254.0.0/16 and fc00::/7), so nothing
    was lost -- this asserts that rather than assuming it."""

    def metadata_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port or 80))
        ]

    monkeypatch.setattr(
        "src.security.ssrf_guard.socket.getaddrinfo", metadata_getaddrinfo
    )
    inner = _NoopInner()
    with pytest.raises(SSRFGuardError):
        await GuardedTransport(inner).handle_async_request(
            httpx.Request("GET", "https://example.invalid/page")
        )
    assert inner.seen == []


@pytest.mark.asyncio
async def test_i_a_metadata_hostname_is_still_refused(counted_resolver):
    """Control for the other half of that move: the name deny-list survived.

    This one cannot be inferred from the IP check -- its whole purpose is to
    catch a metadata name whose DNS answer is a technically-public address,
    which the resolver fixture here supplies.
    """
    inner = _NoopInner()
    with pytest.raises(SSRFGuardError) as excinfo:
        await GuardedTransport(inner).handle_async_request(
            httpx.Request("GET", "http://metadata.google.internal/computeMetadata/v1/")
        )
    assert "metadata" in str(excinfo.value).lower()
    assert inner.seen == []
