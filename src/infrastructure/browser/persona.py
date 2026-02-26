# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Nash Equilibrium Node)
# Role: Dynamic Fingerprint Morphing & Shadow Persona Management.

import random
import hashlib
import json
from typing import Dict, Any, Optional

class PersonaManager:
    """
    Spacescraper Evasion Hub.
    Generates and persists unique hardware fingerprints that reach Nash Stability
    by being statistically indistinguishable from legitimate human users.
    """

    def __init__(self):
        # Deterministic base profiles
        self.os_profiles = ["Windows", "macOS", "Linux"]
        self.screen_resolutions = [
            (1920, 1080), (1440, 900), (1366, 768), (1536, 864), (2560, 1440)
        ]

    def generate_persona(self, persona_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Generates a statistically probable hardware and software fingerprint.
        If persona_id is provided, the fingerprint is deterministic.
        """
        seed = persona_id or str(random.random())
        state = random.Random(seed)

        selected_os = state.choice(self.os_profiles)
        res = state.choice(self.screen_resolutions)
        
        # Scenario 4: Nash Stable Morphing
        # We generate unique constants for WebGL, Canvas, and AudioContext
        # that match the entropy of real-world device clusters.
        fingerprint = {
            "browser_config": {
                "viewport": {"width": res[0], "height": res[1]},
                "user_agent": self._get_ua_for_os(selected_os, state),
                "device_scale_factor": state.choice([1, 1.25, 2]),
                "has_touch": False,
            },
            "evasion_scripts": {
                # These attributes are injected to trick WAF fingerprinting
                "webgl_vendor": state.choice(["Google Inc. (Intel)", "Google Inc. (NVIDIA)", "Apple Inc."]),
                "webgl_renderer": state.choice(["ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0)"]),
                "canvas_bitmask": state.getrandbits(32),
                "audio_pinnacle": state.uniform(1.0, 1.000000001) 
            }
        }
        return fingerprint

    def _get_ua_for_os(self, os_name: str, state: random.Random) -> str:
        """ Returns a modern Chrome UA string for the specific OS. """
        ver = state.randint(120, 122)
        if os_name == "Windows":
            return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36"
        elif os_name == "macOS":
            return f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36"
        return f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36"

persona_manager = PersonaManager()
