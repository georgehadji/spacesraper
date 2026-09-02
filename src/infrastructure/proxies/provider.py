# P3: static proxy list, config-driven. Replaces
# src/infrastructure/proxies/manager.py's ProxySessionManager — that class
# had zero instantiations anywhere and its cookie methods were stubs
# (return []/pass); SessionPool (src/infrastructure/sessions.py) now owns
# session/cookie state instead of a second implementation.
# docs/plans/2026-08-13-capability-enhancement-plan.md P3.

import logging
import os

logger = logging.getLogger("Spacescraper.ProxyProvider")


class StaticProxyProvider:
    """Round-robins a fixed proxy list. Format matches the module it
    replaces: 'http://user:pass@ip:port', 'socks5://ip:port', etc."""

    def __init__(self, proxy_list: list[str] | None = None):
        self.proxy_list = proxy_list or _proxy_list_from_env()
        self._next_index = 0

    def next_proxy(self) -> str | None:
        if not self.proxy_list:
            return None
        proxy = self.proxy_list[self._next_index]
        self._next_index = (self._next_index + 1) % len(self.proxy_list)
        return proxy


def _proxy_list_from_env() -> list[str]:
    raw = os.environ.get("SCRAPER_PROXY_LIST", "")
    return [p.strip() for p in raw.split(",") if p.strip()]
