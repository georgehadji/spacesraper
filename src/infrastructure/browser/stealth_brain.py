# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Nash Strategy Node)
# Role: Collective intelligence for fingerprint evolution.

import logging
from typing import Any

import valkey.asyncio as valkey

from src.config_settings import settings

logger = logging.getLogger("Spacescraper.StealthBrain")

class StealthBrain:
    """
    Spacescraper Evolutionary Engine.
    Tracks successful fingerprint attributes across the entire cluster
    to evolve the "Dominant Persona" strategy.

    Uses a plain Valkey client rather than a queue adapter — this class does
    sorted-set bookkeeping, not message delivery, so it has no business
    depending on queue mechanics (W2.1: one queue mechanism, used for queueing).
    """

    def __init__(self, valkey_url: str | None = None):
        self.valkey_url = valkey_url or settings.valkey.url
        self.valkey: valkey.Valkey | None = None
        try:
            self.valkey = valkey.from_url(self.valkey_url, decode_responses=True)
        except Exception:
            self._setup_mock()
        self.key_prefix = "stealth:evolution"

    def _setup_mock(self):
        try:
            import fakeredis
            self.valkey = fakeredis.FakeAsyncValkey(decode_responses=True)
        except ImportError:
            logger.error("Spacescraper: 'fakeredis' missing. StealthBrain disabled.")
            self.valkey = None

    async def register_success(self, fingerprint: dict[str, Any]):
        """
        Records a successful bypass.
        In a Nash environment, we reinforce the attributes that worked.
        """
        if not self.valkey: return

        # We simplify by tracking successful User-Agents and WebGL renderers
        ua = fingerprint["browser_config"]["user_agent"]
        renderer = fingerprint["evasion_scripts"]["webgl_renderer"]

        try:
            # Increment the success score for this specific combo
            attr_hash = f"{ua}|{renderer}"
            await self.valkey.zincrby(f"{self.key_prefix}:scores", 1, attr_hash)
            logger.debug(f"Spacescraper Brain: Reinforced attribute combo: {renderer[:20]}...")
        except Exception as e:
            logger.debug(f"StealthBrain reinforcement failed: {e}")

    async def get_best_attributes(self) -> dict[str, str] | None:
        """
        Retrieves the statistically most successful attributes from the cluster.
        """
        if not self.valkey: return None
        try:
            best = await self.valkey.zrevrange(f"{self.key_prefix}:scores", 0, 0)
            if best:
                ua, renderer = best[0].split("|")
                return {"user_agent": ua, "webgl_renderer": renderer}
        except:
            pass
        return None

stealth_brain = StealthBrain()
