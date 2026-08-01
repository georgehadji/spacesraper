# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Nash Strategy Node)
# Role: Collective intelligence for fingerprint evolution.

import json
import logging
from typing import Dict, Any, Optional
from src.infrastructure.queues.valkey_worker import ValkeyQueueWorker

logger = logging.getLogger("Spacescraper.StealthBrain")

class StealthBrain:
    """
    Spacescraper Evolutionary Engine.
    Tracks successful fingerprint attributes across the entire cluster 
    to evolve the "Dominant Persona" strategy.
    """
    
    def __init__(self, valkey_worker: Optional[ValkeyQueueWorker] = None):
        self.queue = valkey_worker or ValkeyQueueWorker()
        self.key_prefix = "stealth:evolution"

    async def register_success(self, fingerprint: Dict[str, Any]):
        """
        Records a successful bypass. 
        In a Nash environment, we reinforce the attributes that worked.
        """
        if not self.queue.valkey: return
        
        # We simplify by tracking successful User-Agents and WebGL renderers
        ua = fingerprint["browser_config"]["user_agent"]
        renderer = fingerprint["evasion_scripts"]["webgl_renderer"]
        
        try:
            # Increment the success score for this specific combo
            attr_hash = f"{ua}|{renderer}"
            await self.queue.valkey.zincrby(f"{self.key_prefix}:scores", 1, attr_hash)
            logger.debug(f"Spacescraper Brain: Reinforced attribute combo: {renderer[:20]}...")
        except Exception as e:
            logger.debug(f"StealthBrain reinforcement failed: {e}")

    async def get_best_attributes(self) -> Optional[Dict[str, str]]:
        """
        Retrieves the statistically most successful attributes from the cluster.
        """
        if not self.queue.valkey: return None
        try:
            best = await self.queue.valkey.zrevrange(f"{self.key_prefix}:scores", 0, 0)
            if best:
                ua, renderer = best[0].split("|")
                return {"user_agent": ua, "webgl_renderer": renderer}
        except:
            pass
        return None

stealth_brain = StealthBrain()
