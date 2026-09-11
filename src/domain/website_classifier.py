# Project: Spacescraper (Domain)
# Role: Classify the "website" a business listing advertises.
#
# A directory listing is not a website. Google Maps happily reports a
# vrisko.gr or xo.gr profile in the same `websiteUri` field it uses for a
# real practice site, so a naive "has a website" check counts a yellow-pages
# entry as web presence. This module draws that distinction once, in one
# place, so every caller applies the same rule.

from __future__ import annotations

import re
from enum import Enum
from urllib.parse import urlsplit

__all__ = [
    "WebsiteKind",
    "classify_website",
    "DIRECTORY_DOMAINS",
    "SOCIAL_DOMAINS",
    "BOOKING_DOMAINS",
]


class WebsiteKind(str, Enum):
    """What the advertised URL actually points at."""

    NONE = "none"            # no URL published at all
    DIRECTORY = "directory"  # yellow pages / business directory profile
    SOCIAL = "social"        # social-network profile
    BOOKING = "booking"      # third-party appointment platform
    OWN = "own"              # an independent site the business controls


# Greek yellow pages and the international directories that surface in .gr
# results. `business.site` is Google's own auto-generated Business Profile
# site (retired in 2024, links now bounce back to the profile) -- an artifact
# of the Maps listing, not an independent site, so it belongs here.
DIRECTORY_DOMAINS: frozenset[str] = frozenset({
    # Greece
    "vrisko.gr",
    "xo.gr",
    "chrysosodigos.gr",
    "goldenpages.gr",
    "yellowpages.gr",
    "11888.gr",
    "11880.gr",
    "wowpages.gr",
    "b2bgreece.gr",
    "greekbusiness.gr",
    # International directories that index Greek businesses
    "cylex.gr",
    "cylex-greece.com",
    "tuugo.gr",
    "europages.gr",
    "europages.com",
    "yelp.com",
    "yellowpages.com",
    "foursquare.com",
    "tripadvisor.com",
    "tripadvisor.com.gr",
    "enrollbusiness.com",
    "opendi.gr",
    "infoisinfo.gr",
    "hotfrog.gr",
    # Google-generated Business Profile sites
    "business.site",
})

SOCIAL_DOMAINS: frozenset[str] = frozenset({
    "facebook.com",
    "fb.com",
    "fb.me",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "tiktok.com",
    "pinterest.com",
    "wa.me",
    "t.me",
})

# Appointment/marketplace platforms. A profile here is a booking listing the
# platform owns, not a site the practice controls.
BOOKING_DOMAINS: frozenset[str] = frozenset({
    "doctoranytime.gr",
    "doctoranytime.com",
    "iatronet.gr",
    "iatropedia.gr",
    "e-iatreio.gr",
    "doctorsnet.gr",
    "medicalnet.gr",
    "zocdoc.com",
    "docplanner.com",
})


# A hostname we could actually resolve: dot-separated labels, letters,
# digits and hyphens only. Anything else (a bare word, a phone number typed
# into the website field, free text) is not a site.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$"
)


def _hostname(url: str) -> str | None:
    """Lowercased hostname, with a scheme supplied when the URL omits one."""
    candidate = (url or "").strip()
    if not candidate:
        return None
    # Listings frequently carry a bare "www.example.gr" with no scheme;
    # urlsplit would read that as a path and report no hostname at all.
    if "//" not in candidate.split("?", 1)[0]:
        candidate = f"//{candidate}"
    try:
        host = urlsplit(candidate, scheme="https").hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    # Reject IDN/punycode-free junk before it can be read as a real site.
    return host if _HOSTNAME_RE.match(host) else None


def _matches(host: str, domains: frozenset[str]) -> bool:
    """True when host is one of `domains` or a subdomain of one."""
    return any(host == d or host.endswith(f".{d}") for d in domains)


def classify_website(url: str | None) -> WebsiteKind:
    """
    Classify a listing's advertised URL.

    An unparseable or empty URL is NONE: the caller asked whether the business
    publishes a reachable site, and a value nothing can be made of does not.
    """
    host = _hostname(url or "")
    if host is None:
        return WebsiteKind.NONE
    if _matches(host, DIRECTORY_DOMAINS):
        return WebsiteKind.DIRECTORY
    if _matches(host, SOCIAL_DOMAINS):
        return WebsiteKind.SOCIAL
    if _matches(host, BOOKING_DOMAINS):
        return WebsiteKind.BOOKING
    return WebsiteKind.OWN


def lacks_website(
    url: str | None,
    *,
    social_counts_as_none: bool = True,
    booking_counts_as_none: bool = True,
) -> bool:
    """
    Whether a listing should be treated as having no website.

    NONE and DIRECTORY always count -- a yellow-pages profile is the case the
    caller specifically asked to exclude. SOCIAL and BOOKING are judgement
    calls the caller owns, so they are flags rather than a baked-in rule.
    """
    kind = classify_website(url)
    if kind in (WebsiteKind.NONE, WebsiteKind.DIRECTORY):
        return True
    if kind is WebsiteKind.SOCIAL:
        return social_counts_as_none
    if kind is WebsiteKind.BOOKING:
        return booking_counts_as_none
    return False
