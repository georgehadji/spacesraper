import socket
import pytest
from src.security.ssrf_guard import is_private_ip, validate_outbound_url
from src.domain.exceptions import SSRFGuardError


def _mock_resolve(ip: str):
    """Returns a monkeypatch function that fakes DNS resolution to the given IP."""
    return lambda *a, **kw: [(None, None, None, None, (ip, 0))]


def test_loopback_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolve("127.0.0.1"))
    with pytest.raises(SSRFGuardError) as exc:
        validate_outbound_url("http://internal.example.com/hook")
    assert exc.value.code == "SSRF_BLOCKED"

def test_rfc1918_10_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolve("10.0.0.5"))
    with pytest.raises(SSRFGuardError):
        validate_outbound_url("http://internal.example.com/hook")

def test_rfc1918_172_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolve("172.20.0.1"))
    with pytest.raises(SSRFGuardError):
        validate_outbound_url("http://internal.example.com/hook")

def test_rfc1918_192_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolve("192.168.1.100"))
    with pytest.raises(SSRFGuardError):
        validate_outbound_url("http://internal.example.com/hook")

def test_aws_metadata_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolve("169.254.169.254"))
    with pytest.raises(SSRFGuardError):
        validate_outbound_url("http://internal.example.com/hook")

def test_public_ip_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolve("93.184.216.34"))
    validate_outbound_url("https://example.com/hook")  # must not raise

def test_invalid_scheme_blocked():
    with pytest.raises(SSRFGuardError):
        validate_outbound_url("ftp://example.com/hook")

def test_require_https_rejects_http(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolve("93.184.216.34"))
    with pytest.raises(SSRFGuardError):
        validate_outbound_url("http://example.com/hook", require_https=True)

def test_require_https_accepts_https(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolve("93.184.216.34"))
    validate_outbound_url("https://example.com/hook", require_https=True)  # must not raise

def test_unresolvable_hostname_blocked():
    with pytest.raises(SSRFGuardError):
        validate_outbound_url("http://this-hostname-does-not-exist.invalid/hook")


@pytest.mark.parametrize(
    "ip,why",
    [
        ("0.0.0.0", "0.0.0.0/8 — reaches loopback on Linux"),
        ("0.0.0.1", "0.0.0.0/8"),
        ("::ffff:127.0.0.1", "IPv4-mapped loopback; containment is version-strict"),
        ("::ffff:169.254.169.254", "IPv4-mapped cloud metadata"),
        ("100.64.0.1", "100.64.0.0/10 CGNAT"),
        ("192.0.0.1", "192.0.0.0/24 IETF protocol assignments"),
        ("240.0.0.1", "240.0.0.0/4 reserved"),
        ("::", "unspecified"),
    ],
)
def test_every_private_or_reserved_family_is_rejected(ip, why):
    """The address families the explicit CIDR list omitted. Both egress
    transports gate on this function and then pin the socket to whatever it
    approves, so a False here is a reachable boundary bypass, not a cosmetic
    pre-flight gap."""
    assert is_private_ip(ip) is True, why


@pytest.mark.parametrize("ip", ["93.184.216.34", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_are_still_allowed(ip):
    """The widened classification must not start denying the public internet."""
    assert is_private_ip(ip) is False


def test_ipv4_mapped_loopback_blocked_end_to_end(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mock_resolve("::ffff:127.0.0.1"))
    with pytest.raises(SSRFGuardError):
        validate_outbound_url("http://rebind.example.com/hook")
