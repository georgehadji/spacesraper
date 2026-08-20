# Regression tests for S1: fingerprint coherence.
# validate_fingerprint must accept every build_fingerprint output and reject
# every hand-built incoherent one.

import random

import pytest

from src.domain.fingerprint import (
    OS_PROFILES,
    Fingerprint,
    build_fingerprint,
    validate_fingerprint,
)

CHROMIUM_MAJOR = 130


def test_validate_fingerprint_accepts_1000_seeded_builds_per_profile():
    for profile in OS_PROFILES:
        for seed in range(1000):
            fp = build_fingerprint(CHROMIUM_MAJOR, profile, random.Random(seed))
            errors = validate_fingerprint(fp, CHROMIUM_MAJOR)
            assert errors == [], f"seed={seed} profile={profile.name} errors={errors}"


def test_build_fingerprint_is_deterministic_for_a_given_seed():
    profile = OS_PROFILES[0]
    a = build_fingerprint(CHROMIUM_MAJOR, profile, random.Random("persona-1"))
    b = build_fingerprint(CHROMIUM_MAJOR, profile, random.Random("persona-1"))
    assert a == b


def _base_fp(profile) -> Fingerprint:
    return build_fingerprint(CHROMIUM_MAJOR, profile, random.Random(0))


@pytest.mark.parametrize("mutate,description", [
    (lambda fp: fp.__class__(**{**fp.__dict__, "user_agent": fp.user_agent.replace(
        f"Chrome/{CHROMIUM_MAJOR}.", "Chrome/999.")}), "UA Chrome major mismatches driven Chromium"),
    (lambda fp: fp.__class__(**{**fp.__dict__, "ua_platform": "Windows", "platform": "MacIntel"}),
     "platform disagrees with ua_platform"),
    (lambda fp: fp.__class__(**{**fp.__dict__, "vendor": "Apple Inc.", "renderer": "Apple GPU"}),
     "vendor/renderer pair not plausible for the OS profile (unless already Apple/macOS)"),
    (lambda fp: fp.__class__(**{**fp.__dict__, "screen": (fp.viewport[0] + 1, fp.viewport[1])}),
     "screen disagrees with viewport"),
    (lambda fp: fp.__class__(**{**fp.__dict__, "device_scale_factor": 3}),
     "device_scale_factor outside the plausible set"),
    (lambda fp: fp.__class__(**{**fp.__dict__, "has_touch": True}),
     "has_touch true on a desktop profile"),
])
def test_validate_fingerprint_rejects_hand_built_incoherent_cases(mutate, description):
    profile = OS_PROFILES[0]  # Windows: vendor/renderer mutation above is guaranteed incoherent here
    fp = _base_fp(profile)
    bad = mutate(fp)
    errors = validate_fingerprint(bad, CHROMIUM_MAJOR)
    assert errors != [], f"expected a defect for: {description}"


def test_fingerprint_is_frozen():
    import dataclasses
    fp = _base_fp(OS_PROFILES[0])
    with pytest.raises(dataclasses.FrozenInstanceError):
        fp.user_agent = "tampered"
