# Browser fingerprint value object: a coherent OS/UA/renderer/viewport
# bundle, plus the builder and validator that enforce coherence.
#
# Pure, zero I/O. Chromium's major version and the host OS are read by the
# caller (pool.py / persona.py) and passed in — this module never touches
# the network, the filesystem, or a running browser.

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class OsProfile:
    """The set of fingerprint attributes that must agree, or not at all.

    Sec-CH-UA-Platform is derived by Chromium from the real host OS and
    cannot be set per page — so a Fingerprint's OS must be the host's OS,
    never randomised. See build_fingerprint / validate_fingerprint.
    """

    name: str  # "Windows" | "macOS" | "Linux"
    platform: str  # navigator.platform
    ua_platform: str  # Sec-CH-UA-Platform
    ua_os_token: str  # substring inside the User-Agent string
    renderers: tuple[tuple[str, str], ...]  # (webgl_vendor, webgl_renderer) pairs
    locales: tuple[str, ...]
    timezones: tuple[str, ...]


OS_PROFILES: tuple[OsProfile, ...] = (
    OsProfile(
        name="Windows",
        platform="Win32",
        ua_platform="Windows",
        ua_os_token="Windows NT 10.0; Win64; x64",
        renderers=(
            ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0)"),
            ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0)"),
        ),
        locales=("en-US",),
        timezones=("America/New_York", "America/Chicago", "America/Los_Angeles"),
    ),
    OsProfile(
        name="macOS",
        platform="MacIntel",
        ua_platform="macOS",
        ua_os_token="Macintosh; Intel Mac OS X 10_15_7",
        renderers=(
            ("Apple Inc.", "Apple GPU"),
            ("Apple Inc.", "ANGLE (Apple, Apple M1, OpenGL 4.1)"),
        ),
        locales=("en-US",),
        timezones=("America/Los_Angeles", "America/New_York"),
    ),
    OsProfile(
        name="Linux",
        platform="Linux x86_64",
        ua_platform="Linux",
        ua_os_token="X11; Linux x86_64",
        renderers=(
            ("Google Inc. (Intel)", "ANGLE (Intel, Mesa Intel(R) UHD Graphics 620 (KBL GT2), OpenGL 4.6)"),
        ),
        locales=("en-US",),
        timezones=("America/New_York", "UTC"),
    ),
)

SCREEN_RESOLUTIONS: tuple[tuple[int, int], ...] = (
    (1920, 1080), (1440, 900), (1366, 768), (1536, 864), (2560, 1440),
)

DEVICE_SCALE_FACTORS: tuple[float, ...] = (1, 1.25, 2)


@dataclass(frozen=True)
class Fingerprint:
    user_agent: str
    platform: str
    ua_platform: str
    vendor: str
    renderer: str
    viewport: tuple[int, int]
    screen: tuple[int, int]
    device_scale_factor: float
    has_touch: bool
    hardware_concurrency: int
    device_memory: int
    locale: str
    timezone: str


def profile_by_ua_platform(ua_platform: str) -> OsProfile | None:
    for profile in OS_PROFILES:
        if profile.ua_platform == ua_platform:
            return profile
    return None


def build_fingerprint(chromium_major: int, profile: OsProfile, rng: random.Random) -> Fingerprint:
    """
    Draw order matters: persona_id seeds `rng` deterministically, and
    StealthBrain persists success scores keyed on the resulting UA/renderer
    pair. Adding, removing, or reordering draws changes what every existing
    persona_id maps to.
    """
    resolution = rng.choice(SCREEN_RESOLUTIONS)
    device_scale_factor = rng.choice(DEVICE_SCALE_FACTORS)
    vendor, renderer = rng.choice(profile.renderers)
    locale = rng.choice(profile.locales)
    timezone = rng.choice(profile.timezones)

    user_agent = (
        f"Mozilla/5.0 ({profile.ua_os_token}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chromium_major}.0.0.0 Safari/537.36"
    )

    return Fingerprint(
        user_agent=user_agent,
        platform=profile.platform,
        ua_platform=profile.ua_platform,
        vendor=vendor,
        renderer=renderer,
        viewport=resolution,
        # Playwright's set_viewport_size / new_context(viewport=...) derives
        # window.screen from the viewport itself — no separate screen draw,
        # no anomaly (see docs/plans/2026-08-19 Appendix A: the earlier
        # "impossible viewport/screen pair" claim was refuted).
        screen=resolution,
        device_scale_factor=device_scale_factor,
        has_touch=False,
        hardware_concurrency=4,
        device_memory=8,
        locale=locale,
        timezone=timezone,
    )


def validate_fingerprint(fp: Fingerprint, chromium_major: int) -> list[str]:
    """Returns [] when fp is internally coherent and matches the driven
    Chromium major; otherwise a list of human-readable defects."""
    errors: list[str] = []

    if f"Chrome/{chromium_major}." not in fp.user_agent:
        errors.append(
            f"user_agent Chrome major does not match the driven Chromium major {chromium_major}"
        )

    profile = profile_by_ua_platform(fp.ua_platform)
    if profile is None:
        errors.append(f"unknown ua_platform {fp.ua_platform!r}")
    else:
        if fp.platform != profile.platform:
            errors.append("platform does not match ua_platform's OS profile")
        if profile.ua_os_token not in fp.user_agent:
            errors.append("user_agent OS token does not match ua_platform's OS profile")
        if (fp.vendor, fp.renderer) not in profile.renderers:
            errors.append("vendor/renderer pair is not plausible for this OS profile")
        if fp.locale not in profile.locales:
            errors.append("locale is not in this OS profile's locale pool")
        if fp.timezone not in profile.timezones:
            errors.append("timezone is not in this OS profile's timezone pool")

    if fp.screen != fp.viewport:
        errors.append("screen must equal viewport (Playwright derives screen from viewport)")
    if fp.device_scale_factor not in DEVICE_SCALE_FACTORS:
        errors.append("device_scale_factor outside the plausible set")
    if fp.has_touch:
        errors.append("has_touch=True is incoherent for a desktop profile")

    return errors
