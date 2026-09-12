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
    ipaddress.ip_network("169.254.0.0/16"), # link-local / cloud metadata
    ipaddress.ip_network("::1/128"),        # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),       # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),      # IPv6 link-local
    # Shared address space. CPython deliberately does NOT report this as
    # private (it is globally unreachable but not private), so the stdlib
    # classification in is_private_ip cannot catch it -- yet a carrier-NAT
    # range is internal infrastructure from this process's point of view.
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT (RFC 6598)
]

# Explicit deny-list by name, in addition to the CIDR checks above. Metadata
# endpoints resolve into the 169.254.0.0/16 block on every major cloud today,
# but naming them directly means a future provider quirk (or DNS spoofed to
# answer with a technically-public IP for one of these names) is still caught.
METADATA_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.goog",
})
METADATA_IPS = frozenset({"169.254.169.254", "fd00:ec2::254"})


def is_private_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # fail closed on unparseable IPs

    # An IPv4-mapped IPv6 address (::ffff:127.0.0.1) denotes the IPv4 address
    # it wraps, but ipaddress containment is version-strict, so every IPv4
    # entry in the list below silently answers False for it. Unwrap first.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped

    # The stdlib registry covers the blocks the explicit list omits: 0.0.0.0/8
    # (reaches loopback on Linux), 100.64.0.0/10 (CGNAT), 192.0.0.0/24,
    # 240.0.0.0/4 and ::/128. Both egress transports gate on this function and
    # then pin the socket to the address it approves, so an omission here is a
    # reachable bypass rather than a pre-flight gap. The explicit list is kept
    # as a belt-and-braces check and as documentation of intent.
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_multicast
    ):
        return True
    return any(addr in net for net in _PRIVATE_NETWORKS)


# NOTE: This is a submit-time pre-flight check — a fast-fail for user
# feedback, not the security boundary. The actual fetch happens later, in a
# different process, through http_client's SSRFValidatingTransport
# (src/security/validating_transport.py), which re-resolves and re-checks
# the IP inside the connection attempt and on every redirect hop. A DNS
# rebinding attack can swap the resolved IP between this check and that one,
# which is exactly what the transport closes (F13).
def resolve_and_validate_hostname(hostname: str) -> tuple[str, list[str]]:
    """
    Resolves hostname to IP(s) and validates against private ranges.
    Returns (hostname, [valid_ips]) or raises SSRFGuardError.
    Mitigates DNS rebinding by capturing the resolved IPs once.
    """
    if not hostname:
        raise SSRFGuardError("URL has no resolvable hostname.", code="SSRF_BLOCKED")

    try:
        results = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise SSRFGuardError(
            f"Cannot resolve hostname: {hostname}",
            code="SSRF_BLOCKED",
        )

    valid_ips = []
    for result in results:
        ip = result[4][0]
        if is_private_ip(ip):
            raise SSRFGuardError(
                "URL targets a private or reserved address.",
                code="SSRF_BLOCKED",
            )
        if ip not in valid_ips:
            valid_ips.append(ip)

    if not valid_ips:
        raise SSRFGuardError("No valid public IPs resolved.", code="SSRF_BLOCKED")

    return hostname, valid_ips


def require_supported_url(url: str, *, require_https: bool = False) -> str:
    """
    Check everything about a URL that needs no DNS, and return its hostname.

    Split out of validate_outbound_url so the two egress transports can run
    the URL policy without paying for a lookup they are about to make
    themselves (D14). Both gate on this function, so neither can drift into
    permitting a scheme the other refuses -- which is exactly what had
    happened: GuardedTransport rejected gopher:// and SSRFValidatingTransport
    had no scheme check at all.
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

    return hostname


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
    hostname = require_supported_url(url, require_https=require_https)

    if hostname.lower() in METADATA_HOSTNAMES:
        raise SSRFGuardError(
            f"URL targets a cloud metadata hostname: {hostname}",
            code="SSRF_BLOCKED",
        )

    try:
        results = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise SSRFGuardError(
            f"Cannot resolve hostname: {hostname}",
            code="SSRF_BLOCKED",
        )

    for result in results:
        ip = result[4][0]
        if ip in METADATA_IPS or is_private_ip(ip):
            raise SSRFGuardError(
                "URL targets a private, reserved, or metadata address.",
                code="SSRF_BLOCKED",
            )
