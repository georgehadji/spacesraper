# Author: Spacescraper Security
# Role: httpx transport that enforces the SSRF boundary at actual connection
# time, on every redirect hop — not just at submit time (F13).
#
# ssrf_guard.validate_outbound_url() resolves DNS once, in the API process,
# when a job is submitted. The real fetch happens later, in a different
# process, through a client with follow_redirects=True and no per-hop
# revalidation. That gap allows two live bypasses: DNS rebinding (the name
# resolves to a public IP at submit time, then to a private IP by the time
# the worker connects) and an initially-public URL that 302-redirects to a
# private/metadata address.
#
# This transport closes both: it resolves and validates the hostname itself,
# immediately before connecting, for every request AND every redirect hop
# (httpx re-invokes the transport per hop), then pins the connection to the
# exact IP it just validated — so nothing can change between the check and
# the connect.

import asyncio
import logging
import os
import socket

import httpx

from src.domain.exceptions import SSRFGuardError
from src.security.ssrf_guard import METADATA_HOSTNAMES, METADATA_IPS, is_private_ip

logger = logging.getLogger("Spacescraper.Security.SSRFTransport")


async def _resolve_host(hostname: str, port: int):
    """Isolated for testability — tests monkeypatch this instead of event-loop internals."""
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)


def _enforce_enabled() -> bool:
    """
    Re-read on every call (not cached at import time) so tests and ops can
    flip it without reloading the process.

    Defaults to enforcing (SEC-1b): the previous default was log-only unless
    SSRF_EGRESS_ENFORCE was explicitly set, which meant any deployment path
    that forgot the env var silently ran without a working SSRF guard. Now
    unset means enforce; log-only requires an explicit opt-out.
    """
    return os.environ.get("SSRF_EGRESS_ENFORCE", "true").strip().lower() not in ("0", "false", "no")


class SSRFValidatingTransport(httpx.AsyncHTTPTransport):
    """Drop-in replacement for httpx.AsyncHTTPTransport with an SSRF egress check."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if _enforce_enabled():
            logger.info("SSRF egress guard: enforcing — denied requests are blocked.")
        else:
            logger.warning(
                "SSRF egress guard: log-only (SSRF_EGRESS_ENFORCE explicitly disabled) — "
                "denied requests are logged but allowed through."
            )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        hostname = url.host

        if not hostname:
            raise SSRFGuardError("Request URL has no resolvable hostname.", code="SSRF_BLOCKED")

        if hostname.lower() in METADATA_HOSTNAMES:
            return await self._deny_or_log(request, f"cloud metadata hostname '{hostname}'")

        port = url.port or (443 if url.scheme == "https" else 80)
        try:
            infos = await _resolve_host(hostname, port)
        except socket.gaierror as exc:
            raise SSRFGuardError(f"Cannot resolve hostname: {hostname}", code="SSRF_BLOCKED") from exc

        resolved_ip = infos[0][4][0]
        if resolved_ip in METADATA_IPS or is_private_ip(resolved_ip):
            return await self._deny_or_log(
                request, f"private/reserved/metadata address {resolved_ip} for '{hostname}'"
            )

        # Pin the connection to the IP validated above and carry the original
        # hostname via SNI/Host, so DNS cannot change the outcome after this
        # point (this is what actually defeats rebinding — the check above
        # is only meaningful if the connection is forced to honor it).
        pinned_url = url.copy_with(host=resolved_ip)
        request.url = pinned_url
        request.headers.setdefault("Host", hostname)
        request.extensions["sni_hostname"] = hostname

        return await super().handle_async_request(request)

    async def _deny_or_log(self, request: httpx.Request, reason: str) -> httpx.Response:
        if _enforce_enabled():
            raise SSRFGuardError(f"Egress blocked: {reason}", code="SSRF_BLOCKED")
        logger.warning(
            "SSRF egress guard (log-only, SSRF_EGRESS_ENFORCE unset): would have "
            "blocked request to %s — %s",
            request.url,
            reason,
        )
        return await super().handle_async_request(request)
