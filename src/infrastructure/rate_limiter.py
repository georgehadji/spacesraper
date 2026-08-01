# Per-domain concurrency budgets and rate limiter.
# Uses Valkey for distributed rate limiting across worker nodes.

import logging
import asyncio
from typing import Dict, Optional
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("Spacescraper.DomainRateLimiter")


class DomainRateLimiter:
    """
    Per-domain concurrency budget manager.
    Limits how many concurrent requests can be made to a single domain.
    Supports in-memory (single worker) and Valkey-backed (cluster-wide) modes.
    """

    def __init__(self, default_budget: int = 2, valkey_client=None):
        self.default_budget = default_budget
        self._valkey = valkey_client
        # Per-domain budgets: domain -> max concurrent requests
        self._domain_budgets: Dict[str, int] = {}
        # In-memory semaphores for single-worker mode
        self._semaphores: Dict[str, asyncio.Semaphore] = {}

    def set_budget(self, domain: str, budget: int):
        """Set the concurrency budget for a specific domain."""
        if budget < 1:
            budget = 1
        self._domain_budgets[domain] = budget
        self._semaphores.pop(domain, None)  # recreate on next access

    def get_budget(self, domain: str) -> int:
        """Get the concurrency budget for a domain."""
        return self._domain_budgets.get(domain, self.default_budget)

    def _get_semaphore(self, domain: str) -> asyncio.Semaphore:
        """Get or create a semaphore for a domain."""
        if domain not in self._semaphores:
            budget = self.get_budget(domain)
            self._semaphores[domain] = asyncio.Semaphore(budget)
        return self._semaphores[domain]

    async def acquire(self, domain: str, timeout: Optional[float] = None) -> bool:
        """
        Acquire a concurrency slot for a domain.
        Returns True if slot acquired, False if the budget stayed exhausted.
        Uses Valkey if available for cluster-wide coordination.

        In-memory mode waits on the domain semaphore. With ``timeout=None`` it
        waits indefinitely; with a timeout it gives up and returns False so the
        caller is never blocked past its own deadline.
        """
        if self._valkey:
            return await self._acquire_valkey(domain)
        sem = self._get_semaphore(domain)
        if timeout is None:
            await sem.acquire()
            return True
        try:
            await asyncio.wait_for(sem.acquire(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def release(self, domain: str):
        """Release a concurrency slot for a domain."""
        if self._valkey:
            return  # Valkey tokens expire automatically
        sem = self._semaphores.get(domain)
        if sem:
            sem.release()

    async def _acquire_valkey(self, domain: str) -> bool:
        """Valkey-backed distributed rate limiting."""
        try:
            budget = self.get_budget(domain)
            key = f"rate_limit:domain:{domain}"
            pipe = self._valkey.pipeline()
            now = datetime.now(tz=timezone.utc).timestamp()
            # Remove expired entries
            pipe.zremrangebyscore(key, 0, now - 1.0)
            # Count active
            pipe.zcard(key)
            count = await pipe.execute()
            active = count[1] if count else 0

            if active >= budget:
                return False

            # Add current request
            await self._valkey.zadd(key, {f"req_{now}_{id(self)}": now})
            await self._valkey.expire(key, 10)
            return True
        except Exception as e:
            logger.debug("Valkey rate limit error (allowing): %s", e)
            return True  # fail open

    async def wait_for_slot(self, domain: str, timeout: float = 30.0) -> bool:
        """Wait until a slot becomes available, with timeout."""
        if not self._valkey:
            # The semaphore wakes us the moment a slot frees; no need to poll.
            return await self.acquire(domain, timeout=timeout)
        start = datetime.now(tz=timezone.utc)
        while (datetime.now(tz=timezone.utc) - start).total_seconds() < timeout:
            if await self.acquire(domain):
                return True
            await asyncio.sleep(0.5)
        return False
