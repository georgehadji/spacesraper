"""
Task 1.4 — URL Policy: composable allow/deny rules with robots.txt support.

Implements the Specification pattern for independently testable allow/deny rules.
Deny beats allow (fail-closed default).
"""

import logging
import time
from urllib.parse import urlparse
from typing import Tuple, Optional

import httpx
from src.infrastructure.http_client import HttpClient
from src.infrastructure.cache import LocalLRUCache

logger = logging.getLogger("Spacescraper.UrlPolicy")

# Cache for robots.txt: bounded (LRU-evicted past maxsize, so a long-running
# process crawling many unique domains doesn't grow this without limit) and
# TTL-expiring (so a transient fetch failure doesn't deny a domain forever,
# and a site's robots.txt changes are eventually picked up). Values are
# (expires_at, robots_rules_or_None) tuples; expiry uses time.monotonic().
_ROBOTS_CACHE_TTL_SECONDS = 3600
_ROBOTS_CACHE = LocalLRUCache(maxsize=1000)


class UrlPolicy:
    """
    Composable allow/deny decision for an outbound target.
    Implements Specification pattern: each rule is independently testable.

    Rules (in order of application):
    1. Deny beats allow — denylist always wins
    2. Empty allowlist means "any public host"
    3. robots.txt: Disallow respected, fetch failure depends on trust level
    """

    def __init__(
        self,
        allowlist: Optional[list[str]] = None,
        denylist: Optional[list[str]] = None,
        respect_robots: bool = True,
    ):
        """
        Initialize URL policy.

        Args:
            allowlist: Allowed domains (glob patterns or exact names). None = any public host.
            denylist: Denied domains (glob patterns or exact names). Beats allowlist.
            respect_robots: If True, honor robots.txt Disallow directives.
        """
        self.allowlist = allowlist or []
        self.denylist = denylist or []
        self.respect_robots = respect_robots

    def _domain_matches(self, domain: str, patterns: list[str]) -> bool:
        """Check if domain matches any pattern (exact or wildcard)."""
        domain_lower = domain.lower()
        for pattern in patterns:
            pattern_lower = pattern.lower()
            if "*" in pattern_lower:
                # Simple wildcard: *.example.com
                if pattern_lower.startswith("*."):
                    suffix = pattern_lower[2:]
                    if domain_lower == suffix or domain_lower.endswith(f".{suffix}"):
                        return True
            else:
                # Exact match
                if domain_lower == pattern_lower:
                    return True
        return False

    async def is_allowed(
        self, url: str, *, trust_level: str = "untrusted"
    ) -> Tuple[bool, str]:
        """
        Determine if a URL is allowed to be crawled.

        Args:
            url: URL to check
            trust_level: "trusted" (user-submitted) or "untrusted" (search-derived)

        Returns:
            (decision, reason): (bool, human-readable reason)

        Decision logic:
        1. Deny beats allow
        2. If allowlist non-empty, must be in it
        3. robots.txt: fetch failure = deny for untrusted, allow for trusted
        """
        parsed = urlparse(url)
        # .hostname (not .netloc) strips userinfo and port, so a denylisted
        # host can't be bypassed via user:pass@host or host:nonstandard-port.
        domain = parsed.hostname or parsed.path

        # 1. Denylist check (fail-closed)
        if self.denylist and self._domain_matches(domain, self.denylist):
            return False, f"Domain {domain} in denylist"

        # 2. Allowlist check
        if self.allowlist and not self._domain_matches(domain, self.allowlist):
            return False, f"Domain {domain} not in allowlist"

        # 3. robots.txt check
        if self.respect_robots:
            is_allowed_by_robots, reason = await self._check_robots_txt(
                url, trust_level=trust_level
            )
            if not is_allowed_by_robots:
                return False, reason

        return True, "Allowed"

    async def _check_robots_txt(
        self, url: str, *, trust_level: str = "untrusted"
    ) -> Tuple[bool, str]:
        """
        Check robots.txt Disallow directives.

        Rules:
        - Cache per host (bounded LRU, TTL-expiring — see _ROBOTS_CACHE)
        - Fetch failure: deny for untrusted URLs, allow for trusted
        - Honour Disallow directives for our User-Agent
        """
        parsed = urlparse(url)
        domain = parsed.hostname or parsed.path

        # Check cache first (skip — and fall through to re-fetch — on a miss
        # or an expired entry; see _ROBOTS_CACHE's TTL/bound docstring)
        cached = _ROBOTS_CACHE.get(domain)
        if cached is not None and time.monotonic() < cached[0]:
            robots_rules = cached[1]
            if robots_rules is None:
                # Cached failure
                if trust_level == "untrusted":
                    return False, f"robots.txt fetch failed for {domain} (denying untrusted)"
                else:
                    return True, f"robots.txt fetch failed for {domain} (allowing trusted)"

            # Check Disallow directives
            path = parsed.path or "/"
            disallows = robots_rules.get("disallow", [])
            for disallow in disallows:
                if path.startswith(disallow):
                    return False, f"robots.txt Disallow: {disallow}"
            return True, "robots.txt: allowed"

        # Fetch robots.txt
        robots_url = f"{parsed.scheme or 'https'}://{domain}/robots.txt"
        try:
            client = await HttpClient.get_client(allow_private=False)
            response = await client.get(robots_url, timeout=5.0)

            if response.status_code == 200:
                robots_rules = self._parse_robots_txt(response.text)
                _ROBOTS_CACHE.set(domain, (time.monotonic() + _ROBOTS_CACHE_TTL_SECONDS, robots_rules))

                # Check Disallow directives
                path = parsed.path or "/"
                disallows = robots_rules.get("disallow", [])
                for disallow in disallows:
                    if path.startswith(disallow):
                        return False, f"robots.txt Disallow: {disallow}"
                return True, "robots.txt: allowed"
            else:
                # Not found or error — cache the failure
                _ROBOTS_CACHE.set(domain, (time.monotonic() + _ROBOTS_CACHE_TTL_SECONDS, None))
                if trust_level == "untrusted":
                    return False, f"robots.txt not found for {domain} (denying untrusted)"
                else:
                    return True, f"robots.txt not found for {domain} (allowing trusted)"

        except Exception as e:
            logger.warning(f"robots.txt fetch failed for {domain}: {e}")
            _ROBOTS_CACHE.set(domain, (time.monotonic() + _ROBOTS_CACHE_TTL_SECONDS, None))
            if trust_level == "untrusted":
                return False, f"robots.txt fetch error for {domain}: {e}"
            else:
                return True, f"robots.txt fetch error for {domain}: {e}"

    @staticmethod
    def _parse_robots_txt(content: str) -> dict[str, list[str]]:
        """
        Parse robots.txt and extract Disallow directives for our User-Agent.

        Simple parser: looks for "User-agent: *" and "Disallow: /path" lines.
        Returns dict with "disallow" key containing list of paths.
        """
        disallows = []
        in_user_agent_section = False

        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if line.lower().startswith("user-agent:"):
                agent = line.split(":", 1)[1].strip()
                in_user_agent_section = agent == "*"
            elif line.lower().startswith("disallow:") and in_user_agent_section:
                path = line.split(":", 1)[1].strip()
                if path:
                    disallows.append(path)

        return {"disallow": disallows}
