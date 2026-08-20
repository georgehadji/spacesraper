# Regression tests for S2: HTTP client split by trust direction.
# internal_http (webhooks, LLM API) must send an honest, neutral UA.
# target_http (scrape targets) must send a Fingerprint-sourced UA. Neither
# may leak the "Spacescraper" bot signature or the author's personal name.

import pytest

from src.infrastructure.http_client import internal_http, target_http
from src.security import validating_transport as vt_module


def _outbound_user_agent(client_headers) -> str:
    return client_headers.get("user-agent", "")


@pytest.mark.asyncio
async def test_internal_http_ua_is_honest_and_neutral():
    client = await internal_http.get_client()
    ua = _outbound_user_agent(client.headers)
    assert "Spacescraper" not in ua
    assert "Georgios" not in ua
    assert "Chatzivantsidis" not in ua


@pytest.mark.asyncio
async def test_target_http_ua_is_fingerprint_sourced_not_a_literal():
    client = await target_http.get_client()
    ua = _outbound_user_agent(client.headers)
    assert "Spacescraper" not in ua
    assert "Georgios" not in ua
    assert "Chatzivantsidis" not in ua
    # A real Fingerprint UA, not a bot-signature literal.
    assert "Chrome/" in ua
    assert "Mozilla/5.0" in ua


@pytest.mark.asyncio
async def test_both_clients_use_ssrf_validating_transport():
    for c in (await internal_http.get_client(), await target_http.get_client()):
        assert isinstance(c._transport, vt_module.SSRFValidatingTransport)
