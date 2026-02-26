# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Security — SSRF Guard)
# Role: Validates outbound URLs to prevent Server-Side Request Forgery attacks.

import ipaddress
import socket
from urllib.parse import urlparse

from src.domain.exceptions import SSRFGuardError

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),    # loopback
    ipaddress.ip_network("10.0.0.0/8"),     # RFC1918
    ipaddress.ip_network("172.16.0.0/12"),  # RFC1918
    ipaddress.ip_network("192.168.0.0/16"), # RFC1918
    ipaddress.ip_network("169.254.0.0/16"), # link-local / AWS metadata
    ipaddress.ip_network("::1/128"),        # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),       # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),      # IPv6 link-local
]


def _is_private_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        return True  # fail closed on unparseable IPs


# NOTE: This guard is a pre-flight check only. A DNS rebinding attack can
# swap the resolved IP between this validation and the actual HTTP request.
# For complete protection, pair this guard with an HTTP client that re-resolves
# and re-checks the IP inside the connection attempt.
def validate_outbound_url(url: str, *, require_https: bool = False) -> None:
    """
    Validates that `url` is safe to use as an outbound HTTP destination.

    Raises SSRFGuardError if:
    - The URL scheme is not http or https
    - require_https=True and scheme is http
    - The hostname resolves to a private/reserved IP address
    - The hostname cannot be resolved

    Usage:
        validate_outbound_url(webhook_url)  # raises SSRFGuardError on violation
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise SSRFGuardError(
            f"URL scheme '{parsed.scheme}' is not allowed. Use http or https.",
            code="SSRF_BLOCKED",
        )

    if require_https and parsed.scheme != "https":
        raise SSRFGuardError(
            "HTTPS is required for outbound webhook URLs in production.",
            code="SSRF_BLOCKED",
        )

    hostname = parsed.hostname
    if not hostname:
        raise SSRFGuardError("URL has no resolvable hostname.", code="SSRF_BLOCKED")

    try:
        results = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise SSRFGuardError(
            f"Cannot resolve hostname: {hostname}",
            code="SSRF_BLOCKED",
        )

    for result in results:
        ip = result[4][0]
        if _is_private_ip(ip):
            raise SSRFGuardError(
                "URL targets a private or reserved address.",
                code="SSRF_BLOCKED",
            )
