"""
Tests for Task 1.4 — URL Policy with robots.txt support.
Verifies composable allow/deny rules with fail-closed semantics.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.security.url_policy import UrlPolicy


class TestUrlPolicyBasicRules:
    """Test basic allow/deny rules (no robots.txt)."""

    @pytest.mark.asyncio
    async def test_empty_allowlist_means_any_public_host(self):
        """Empty allowlist allows any domain (later blocked by robots.txt or SSRF guard)."""
        policy = UrlPolicy(allowlist=[], denylist=[], respect_robots=False)

        allowed, _ = await policy.is_allowed("https://example.com/")
        assert allowed is True

        allowed, _ = await policy.is_allowed("https://news.ycombinator.com/")
        assert allowed is True

    @pytest.mark.asyncio
    async def test_allowlist_non_empty_requires_match(self):
        """Non-empty allowlist must match or request is denied."""
        policy = UrlPolicy(
            allowlist=["example.com", "*.trusted.io"],
            denylist=[],
            respect_robots=False,
        )

        # Allowed
        allowed, _ = await policy.is_allowed("https://example.com/")
        assert allowed is True

        allowed, _ = await policy.is_allowed("https://sub.trusted.io/")
        assert allowed is True

        # Not allowed
        allowed, reason = await policy.is_allowed("https://untrusted.com/")
        assert allowed is False
        assert "not in allowlist" in reason

    @pytest.mark.asyncio
    async def test_denylist_beats_allowlist(self):
        """Denylist always wins (deny beats allow)."""
        policy = UrlPolicy(
            allowlist=["example.com"],
            denylist=["*.internal.example.com"],
            respect_robots=False,
        )

        # example.com is in allowlist but sub.internal.example.com is denied
        allowed, reason = await policy.is_allowed("https://sub.internal.example.com/")
        assert allowed is False
        assert "denylist" in reason

    @pytest.mark.asyncio
    async def test_wildcard_patterns(self):
        """Test wildcard matching."""
        policy = UrlPolicy(
            allowlist=["*.good.io", "specific.com"],
            denylist=[],
            respect_robots=False,
        )

        # Wildcard matches
        allowed, _ = await policy.is_allowed("https://news.good.io/")
        assert allowed is True

        allowed, _ = await policy.is_allowed("https://api.good.io/")
        assert allowed is True

        # Exact match
        allowed, _ = await policy.is_allowed("https://specific.com/")
        assert allowed is True

        # No match
        allowed, reason = await policy.is_allowed("https://evil.io/")
        assert allowed is False


class TestUrlPolicyRobotsTxt:
    """Test robots.txt handling."""

    @pytest.mark.asyncio
    async def test_robots_txt_disallow_respected(self):
        """robots.txt Disallow directives are honored."""
        policy = UrlPolicy(
            allowlist=[],
            denylist=[],
            respect_robots=True,
        )

        robots_content = """
User-agent: *
Disallow: /admin
Disallow: /private
"""

        with patch.object(policy, "_check_robots_txt") as mock_robots:
            # Simulate robots.txt allowing /public
            mock_robots.return_value = (True, "robots.txt: allowed")
            allowed, _ = await policy.is_allowed("https://example.com/public/page")
            assert allowed is True

            # Simulate robots.txt blocking /admin
            mock_robots.return_value = (False, "robots.txt Disallow: /admin")
            allowed, reason = await policy.is_allowed("https://example.com/admin")
            assert allowed is False
            assert "Disallow" in reason

    @pytest.mark.asyncio
    async def test_robots_txt_fetch_failure_denies_untrusted(self):
        """robots.txt fetch failure denies untrusted (search-derived) URLs."""
        policy = UrlPolicy(
            allowlist=[],
            denylist=[],
            respect_robots=True,
        )

        with patch.object(policy, "_check_robots_txt") as mock_robots:
            # robots.txt fetch failed for untrusted URL
            mock_robots.return_value = (
                False,
                "robots.txt fetch failed (denying untrusted)",
            )
            allowed, reason = await policy.is_allowed(
                "https://example.com/", trust_level="untrusted"
            )
            assert allowed is False
            assert "fetch failed" in reason

    @pytest.mark.asyncio
    async def test_robots_txt_fetch_failure_allows_trusted(self):
        """robots.txt fetch failure allows trusted (user-submitted) URLs."""
        policy = UrlPolicy(
            allowlist=[],
            denylist=[],
            respect_robots=True,
        )

        with patch.object(policy, "_check_robots_txt") as mock_robots:
            # robots.txt fetch failed for trusted URL
            mock_robots.return_value = (
                True,
                "robots.txt fetch failed (allowing trusted)",
            )
            allowed, reason = await policy.is_allowed(
                "https://example.com/", trust_level="trusted"
            )
            assert allowed is True

    def test_parse_robots_txt(self):
        """Test robots.txt parsing."""
        robots_content = """
# Comment line
User-agent: *
Disallow: /admin
Disallow: /api/v1
Allow: /api/v1/public

User-agent: Googlebot
Disallow: /secret
"""

        result = UrlPolicy._parse_robots_txt(robots_content)

        assert "disallow" in result
        assert "/admin" in result["disallow"]
        assert "/api/v1" in result["disallow"]
        # Only applies to User-agent: *
        assert "/secret" not in result["disallow"]

    def test_robots_txt_empty_content(self):
        """Empty robots.txt should parse cleanly."""
        result = UrlPolicy._parse_robots_txt("")
        assert result == {"disallow": []}

    def test_robots_txt_malformed(self):
        """Malformed robots.txt should not crash."""
        robots_content = """
User-agent *
Disallow /admin
"""
        result = UrlPolicy._parse_robots_txt(robots_content)
        # Should handle gracefully
        assert "disallow" in result


class TestUrlPolicyCombined:
    """Test combined rules (allowlist + denylist + robots)."""

    @pytest.mark.asyncio
    async def test_full_pipeline(self):
        """Full pipeline: denylist → allowlist → robots."""
        policy = UrlPolicy(
            allowlist=["*.example.com"],
            denylist=["evil.example.com"],
            respect_robots=True,
        )

        # Blocked by denylist
        allowed, reason = await policy.is_allowed("https://evil.example.com/page")
        assert allowed is False

        # Allowed by allowlist, then robots check (mocked)
        with patch.object(policy, "_check_robots_txt") as mock_robots:
            mock_robots.return_value = (True, "robots: allowed")
            allowed, _ = await policy.is_allowed("https://good.example.com/page")
            assert allowed is True

    @pytest.mark.asyncio
    async def test_case_insensitive_matching(self):
        """Domain matching should be case-insensitive."""
        policy = UrlPolicy(
            allowlist=["Example.COM"],
            denylist=[],
            respect_robots=False,
        )

        # Different case should still match
        allowed, _ = await policy.is_allowed("https://EXAMPLE.com/")
        assert allowed is True

        allowed, _ = await policy.is_allowed("https://example.COM/")
        assert allowed is True


class TestUrlPolicyHostnameNotNetloc:
    """
    F3 regression: domain matching must use parsed.hostname (bare host),
    not parsed.netloc (which also carries userinfo and port). Matching on
    netloc lets a denylisted host slip through via a nonstandard port or a
    user:pass@ prefix, and — the same bug from the other side — wrongly
    rejects a legitimately allowlisted host reached on a nonstandard port.
    """

    @pytest.mark.asyncio
    async def test_denylist_not_bypassed_by_nonstandard_port(self):
        policy = UrlPolicy(
            allowlist=[],
            denylist=["internal.example.com"],
            respect_robots=False,
        )

        allowed, reason = await policy.is_allowed(
            "https://internal.example.com:8443/admin"
        )
        assert allowed is False
        assert "denylist" in reason

    @pytest.mark.asyncio
    async def test_denylist_not_bypassed_by_userinfo_prefix(self):
        policy = UrlPolicy(
            allowlist=[],
            denylist=["internal.example.com"],
            respect_robots=False,
        )

        allowed, reason = await policy.is_allowed(
            "https://attacker.com@internal.example.com/admin"
        )
        assert allowed is False
        assert "denylist" in reason

    @pytest.mark.asyncio
    async def test_allowlist_matches_despite_nonstandard_port(self):
        """A legitimately allowlisted host must not be rejected just because
        the URL carries a nonstandard port."""
        policy = UrlPolicy(
            allowlist=["example.com"],
            denylist=[],
            respect_robots=False,
        )

        allowed, _ = await policy.is_allowed("https://example.com:8080/")
        assert allowed is True


class TestUrlPolicyRobotsCacheBounds:
    """
    F1 regression: the robots.txt cache must be TTL-expiring and
    size-bounded, not a plain dict that grows for the life of the process
    and never re-checks a domain once cached — including a cached fetch
    failure, which would otherwise deny that domain forever.
    """

    @pytest.mark.asyncio
    async def test_robots_cache_expires_after_ttl(self, monkeypatch):
        import src.security.url_policy as url_policy_module

        policy = UrlPolicy(respect_robots=True)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "User-agent: *\nDisallow: /admin\n"

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        # Every entry reads as already-expired the instant it's written.
        monkeypatch.setattr(url_policy_module, "_ROBOTS_CACHE_TTL_SECONDS", -1)

        with patch.object(
            url_policy_module.HttpClient,
            "get_client",
            AsyncMock(return_value=mock_client),
        ):
            await policy._check_robots_txt("https://ttl-test.example.com/page")
            await policy._check_robots_txt("https://ttl-test.example.com/page")

        # An expired entry must never be served as if still live — both
        # calls should have gone to the network.
        assert mock_client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_robots_cache_is_bounded(self, monkeypatch):
        import src.security.url_policy as url_policy_module
        from src.infrastructure.cache import LocalLRUCache

        # Fresh, tiny cache so eviction is observable without 1000+ inserts.
        monkeypatch.setattr(url_policy_module, "_ROBOTS_CACHE", LocalLRUCache(maxsize=2))

        policy = UrlPolicy(respect_robots=True)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "User-agent: *\n"

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch.object(
            url_policy_module.HttpClient,
            "get_client",
            AsyncMock(return_value=mock_client),
        ):
            await policy._check_robots_txt("https://first.example.com/")
            await policy._check_robots_txt("https://second.example.com/")
            await policy._check_robots_txt("https://third.example.com/")  # evicts "first"
            assert mock_client.get.call_count == 3

            # "first" was evicted (maxsize=2) — checking it again must
            # re-fetch rather than come from a cache that grew unbounded.
            await policy._check_robots_txt("https://first.example.com/")
            assert mock_client.get.call_count == 4
