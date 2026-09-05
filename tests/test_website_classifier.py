"""A directory profile is not a website. These tests pin that distinction."""

import pytest

from src.domain.website_classifier import (
    WebsiteKind,
    classify_website,
    lacks_website,
)


@pytest.mark.parametrize(
    "url,expected",
    [
        # Nothing published.
        (None, WebsiteKind.NONE),
        ("", WebsiteKind.NONE),
        ("   ", WebsiteKind.NONE),
        # Greek yellow pages -- the case this module exists for. Both appear
        # verbatim in the Thermaikos sweep.
        ("https://www.xo.gr/profile/profile-911524", WebsiteKind.DIRECTORY),
        ("https://www.vrisko.gr/advdetails/abc", WebsiteKind.DIRECTORY),
        ("http://xo.gr/x", WebsiteKind.DIRECTORY),
        ("https://11888.gr/listing/1", WebsiteKind.DIRECTORY),
        # Google's own auto-generated Business Profile site.
        ("https://my-clinic.business.site/", WebsiteKind.DIRECTORY),
        # Social and booking platforms are separate verdicts, not directories.
        ("https://m.facebook.com/pages/iatreio", WebsiteKind.SOCIAL),
        ("https://www.instagram.com/dr_x/", WebsiteKind.SOCIAL),
        ("https://www.doctoranytime.gr/d/kardiologos/x", WebsiteKind.BOOKING),
        # A real practice site.
        ("https://drpapadopoulos.gr", WebsiteKind.OWN),
        ("www.iatreio-perea.gr", WebsiteKind.OWN),
    ],
)
def test_classification(url, expected):
    assert classify_website(url) is expected


def test_case_and_subdomains_do_not_evade_the_directory_check():
    assert classify_website("HTTPS://WWW.XO.GR/A") is WebsiteKind.DIRECTORY
    assert classify_website("https://a.b.vrisko.gr/x") is WebsiteKind.DIRECTORY


def test_suffix_match_respects_the_label_boundary():
    """`notvrisko.gr` merely ends with the string; it is a different domain."""
    assert classify_website("https://notvrisko.gr") is WebsiteKind.OWN


@pytest.mark.parametrize("junk", ["not a url", "2310123456", "localhost", "n/a"])
def test_unparseable_values_count_as_no_website(junk):
    """
    Garbage in websiteUri must not read as a real site: doing so would drop a
    business from the "needs a website" list, the costlier error direction.
    """
    assert classify_website(junk) is WebsiteKind.NONE
    assert lacks_website(junk) is True


def test_directory_always_counts_as_missing_regardless_of_flags():
    assert lacks_website(
        "https://www.vrisko.gr/x",
        social_counts_as_none=False,
        booking_counts_as_none=False,
    ) is True


def test_social_and_booking_are_caller_decisions():
    fb = "https://facebook.com/clinic"
    booking = "https://doctoranytime.gr/d/x"
    assert lacks_website(fb, social_counts_as_none=True) is True
    assert lacks_website(fb, social_counts_as_none=False) is False
    assert lacks_website(booking, booking_counts_as_none=True) is True
    assert lacks_website(booking, booking_counts_as_none=False) is False


def test_own_site_is_never_missing():
    assert lacks_website("https://drpapadopoulos.gr") is False
