"""
Tests for Task 1.4 — URL Policy with robots.txt support.
Verifies composable allow/deny rules with fail-closed semantics.
"""

import pytest
from unittest.mock import AsyncMock, patch

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
