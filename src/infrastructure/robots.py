# P2: robots.txt gate — fail-closed on fetch errors, allow-all on a missing
# file (404), stdlib parser. docs/plans/2026-08-13-capability-enhancement-plan.md
# P2 (RobotsPort, R4).
#
# In-memory per-domain TTL cache — a multi-replica worker deployment would
# want this shared (Valkey, TTL key per domain) so every replica doesn't
# refetch the same robots.txt; out of scope for a single-process gate.

import logging
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from src.infrastructure.http_client import target_http

logger = logging.getLogger("Spacescraper.Robots")

DEFAULT_TTL_S = 3600
USER_AGENT = "*"


class HttpRobotsGate:
    def __init__(self, ttl_s: float = DEFAULT_TTL_S):
        self.ttl_s = ttl_s
        self._cache: dict[str, tuple[float, RobotFileParser | None]] = {}

    async def _get_parser(self, url: str) -> RobotFileParser | None:
        """None means "robots.txt could not be determined" — the caller
        fails closed on that, never on a confirmed-empty/404 file."""
        origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        cached = self._cache.get(origin)
        if cached is not None and time.monotonic() - cached[0] < self.ttl_s:
            return cached[1]

        parser: RobotFileParser | None
        try:
            response = await target_http.get(f"{origin}/robots.txt")
        except Exception:
            logger.debug("robots.txt fetch failed for %s", origin, exc_info=True)
            parser = None
        else:
            if response.status_code == 404:
                parser = RobotFileParser()
                parser.parse("".splitlines())
            elif response.status_code >= 400:
                parser = None
            else:
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())

        self._cache[origin] = (time.monotonic(), parser)
        return parser

    async def is_allowed(self, url: str) -> bool:
        parser = await self._get_parser(url)
        if parser is None:
            return False  # fail closed (R4)
        return parser.can_fetch(USER_AGENT, url)

    async def crawl_delay_seconds(self, url: str) -> float | None:
        parser = await self._get_parser(url)
        if parser is None:
            return None
        delay = parser.crawl_delay(USER_AGENT)
        return float(delay) if delay is not None else None
