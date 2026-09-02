# R-W7.4/R16 (docs/plans/2026-09-01-review-remediation.md): StaticProxyProvider
# replaced proxies/manager.py's ProxySessionManager (W6-era deletion, confirmed
# zero other callers), but nothing exercised its own actual behavior —
# round-robin wraparound, the empty-list case, or SCRAPER_PROXY_LIST parsing.
# tests/test_sessions.py only ever used a FakeProxyProvider double.

from src.infrastructure.proxies.provider import StaticProxyProvider, _proxy_list_from_env


class TestStaticProxyProviderRoundRobin:
    def test_cycles_through_list_in_order(self):
        provider = StaticProxyProvider(["http://p1:8080", "http://p2:8080", "http://p3:8080"])
        assert provider.next_proxy() == "http://p1:8080"
        assert provider.next_proxy() == "http://p2:8080"
        assert provider.next_proxy() == "http://p3:8080"

    def test_wraps_around_after_the_last_entry(self):
        provider = StaticProxyProvider(["http://p1:8080", "http://p2:8080"])
        provider.next_proxy()  # p1
        provider.next_proxy()  # p2
        assert provider.next_proxy() == "http://p1:8080", "must wrap back to the first entry"
        assert provider.next_proxy() == "http://p2:8080"

    def test_single_entry_list_always_returns_that_entry(self):
        provider = StaticProxyProvider(["http://only:8080"])
        for _ in range(5):
            assert provider.next_proxy() == "http://only:8080"

    def test_empty_list_returns_none_without_error(self):
        provider = StaticProxyProvider([])
        assert provider.next_proxy() is None
        assert provider.next_proxy() is None, "must not raise or wrap on repeated calls"


class TestProxyListFromEnv:
    def test_missing_env_var_yields_empty_list(self, monkeypatch):
        monkeypatch.delenv("SCRAPER_PROXY_LIST", raising=False)
        assert _proxy_list_from_env() == []

    def test_empty_env_var_yields_empty_list(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PROXY_LIST", "")
        assert _proxy_list_from_env() == []

    def test_splits_on_commas(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PROXY_LIST", "http://p1:8080,http://p2:8080,http://p3:8080")
        assert _proxy_list_from_env() == ["http://p1:8080", "http://p2:8080", "http://p3:8080"]

    def test_strips_surrounding_whitespace_per_entry(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PROXY_LIST", " http://p1:8080 , http://p2:8080  ")
        assert _proxy_list_from_env() == ["http://p1:8080", "http://p2:8080"]

    def test_drops_empty_entries_from_trailing_or_double_commas(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PROXY_LIST", "http://p1:8080,,http://p2:8080,")
        assert _proxy_list_from_env() == ["http://p1:8080", "http://p2:8080"]

    def test_single_entry_no_commas(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PROXY_LIST", "http://p1:8080")
        assert _proxy_list_from_env() == ["http://p1:8080"]


class TestStaticProxyProviderReadsEnvOnConstruction:
    def test_uses_env_var_when_no_explicit_list_given(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PROXY_LIST", "http://envproxy:8080")
        provider = StaticProxyProvider()
        assert provider.next_proxy() == "http://envproxy:8080"

    def test_explicit_list_overrides_env_var(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PROXY_LIST", "http://envproxy:8080")
        provider = StaticProxyProvider(["http://explicit:8080"])
        assert provider.next_proxy() == "http://explicit:8080"
