# R-W4/R6 (docs/plans/2026-09-01-review-remediation.md): an authenticated
# proxy URL ('http://user:pass@ip:port') worked on Tier 1 (curl_cffi, which
# parses embedded userinfo natively) but silently lost its credentials on
# Tier 2 (Playwright, which needs them as separate username/password keys —
# passing the whole URL as `server` just drops them, and the proxy answers
# 407). This file proves both tiers now handle the same proxy string
# correctly: parse_proxy_url for Tier 2, and a pass-through check for Tier 1
# so a future change can't quietly break the tier that already worked.

import pytest

from src.infrastructure.browser.pool import parse_proxy_url


class TestParseProxyUrl:
    def test_splits_credentials_into_separate_keys(self):
        result = parse_proxy_url("http://scraperuser:s3cr3t@proxy.example.com:8080")
        assert result == {
            "server": "http://proxy.example.com:8080",
            "username": "scraperuser",
            "password": "s3cr3t",
        }

    def test_no_credentials_yields_server_only(self):
        result = parse_proxy_url("http://proxy.example.com:8080")
        assert result == {"server": "http://proxy.example.com:8080"}
        assert "username" not in result
        assert "password" not in result

    def test_socks5_scheme_preserved(self):
        result = parse_proxy_url("socks5://user:pass@10.0.0.1:1080")
        assert result["server"] == "socks5://10.0.0.1:1080"
        assert result["username"] == "user"
        assert result["password"] == "pass"

    def test_no_port_omits_it_from_server(self):
        result = parse_proxy_url("http://user:pass@proxy.example.com")
        assert result["server"] == "http://proxy.example.com"

    def test_credentials_with_special_characters(self):
        # '@' and ':' inside a password are common (generated secrets); the
        # proxy string itself must already be percent-encoded by whoever
        # configured SCRAPER_PROXY_LIST — urlsplit decodes standard percent
        # escapes, it does not need bare special characters to work.
        result = parse_proxy_url("http://user:p%40ss%3Aword@proxy.example.com:3128")
        assert result["username"] == "user"
        assert result["password"] == "p@ss:word"


class TestTier1ProxyPassthrough:
    """Tier 1 (curl_cffi) was already correct — this locks it in."""

    @pytest.mark.asyncio
    async def test_impersonating_http_fetcher_passes_proxy_string_unchanged(self, monkeypatch):
        from src.domain.fetch import FetchRequest
        from src.infrastructure.fetch import http_fetcher as http_fetcher_module
        from src.infrastructure.fetch.http_fetcher import ImpersonatingHttpFetcher

        monkeypatch.setattr(http_fetcher_module, "validate_outbound_url", lambda url: None)

        captured = {}

        class _FakeResponse:
            status_code = 200
            text = "<html></html>"
            headers = {}

        class _FakeAsyncSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, *, impersonate, timeout, proxy):
                captured["proxy"] = proxy
                return _FakeResponse()

        monkeypatch.setattr(http_fetcher_module, "AsyncSession", _FakeAsyncSession)

        proxy_url = "http://scraperuser:s3cr3t@proxy.example.com:8080"
        fetcher = ImpersonatingHttpFetcher()
        await fetcher.fetch(FetchRequest(url="https://example.com", proxy=proxy_url))

        assert captured["proxy"] == proxy_url, (
            "curl_cffi's own proxy= kwarg must receive the full URL with "
            "embedded credentials unchanged — it parses userinfo natively, "
            "unlike Playwright's server field."
        )
