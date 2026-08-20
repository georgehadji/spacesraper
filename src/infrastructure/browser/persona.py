# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Nash Equilibrium Node)
# Role: Deterministic fingerprint generation, pinned to the host OS.

import platform
import random

from src.domain.fingerprint import OS_PROFILES, Fingerprint, OsProfile, build_fingerprint

_SYSTEM_TO_PROFILE_NAME = {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}


def detect_host_os_profile() -> OsProfile:
    """
    Sec-CH-UA-Platform is derived by Chromium from the real host OS and
    cannot be set per page. Claiming a different OS in the UA/persona than
    the client hints report is exactly the kind of mismatch S1 exists to
    remove, so the profile is always the host's, never randomised.
    """
    name = _SYSTEM_TO_PROFILE_NAME.get(platform.system(), "Linux")
    for profile in OS_PROFILES:
        if profile.name == name:
            return profile
    return OS_PROFILES[0]


class PersonaManager:
    """
    Generates a coherent Fingerprint per persona_id. Deterministic: the same
    persona_id always yields the same Fingerprint, so StealthBrain's success
    scores (keyed on the resulting user_agent|renderer pair) stay meaningful
    across runs.
    """

    def generate_fingerprint(self, persona_id: str | None, chromium_major: int) -> Fingerprint:
        seed = persona_id or str(random.random())
        rng = random.Random(seed)
        profile = detect_host_os_profile()
        return build_fingerprint(chromium_major, profile, rng)


persona_manager = PersonaManager()
