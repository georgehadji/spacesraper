# Strategy auto-selector — periodically evaluates strategies per domain
# and updates DomainProfiles with the best-performing strategy.

import asyncio
import logging
from typing import Optional, Set
from datetime import datetime, timedelta

from src.infrastructure.repositories.observation_repository import SqliteObservationRepository
from src.application.evaluator import StrategyEvaluator

logger = logging.getLogger("Spacescraper.StrategySelector")

DEFAULT_EVAL_INTERVAL = 3600  # 1 hour between full evaluations
DEFAULT_MIN_OBS = 10  # minimum observations before evaluation


class StrategySelector:
    """
    Periodically evaluates extraction strategies per domain and
    updates DomainProfiles with the best performer.
    Can run as a background task or be triggered on-demand.
    """

    def __init__(self, obs_repo: SqliteObservationRepository):
        self.repo = obs_repo
        self.evaluator = StrategyEvaluator(repo=obs_repo)

    async def evaluate_all_domains(
        self, min_observations: int = DEFAULT_MIN_OBS,
    ) -> int:
        """
        Evaluate strategies for all domains that have sufficient observations.
        Returns the number of domains processed.
        """
        # Get distinct domains from observations
        domains = await self._get_domains_with_observations(min_observations)
        if not domains:
            logger.info("StrategySelector: No domains with >= %d observations", min_observations)
            return 0

        count = 0
        for domain in domains:
            try:
                profile = await self.evaluator.update_domain_profile(domain)
                if profile:
                    logger.info(
                        "StrategySelector: Domain %s -> best strategy: %s (success=%.0f%%, block=%.0f%%)",
                        domain, profile.preferred_strategy,
                        profile.success_rate * 100, profile.block_rate * 100,
                    )
                    count += 1
            except Exception as e:
                logger.warning("StrategySelector: Error evaluating domain %s: %s", domain, e)

        return count

    async def evaluate_domain(self, domain: str) -> None:
        """Evaluate and update a single domain's profile."""
        profile = await self.evaluator.update_domain_profile(domain)
        if profile:
            logger.info(
                "StrategySelector: %s -> %s (%.0f%% success, %d obs)",
                domain, profile.preferred_strategy,
                profile.success_rate * 100, profile.total_observations,
            )

    async def run_forever(self, interval: float = DEFAULT_EVAL_INTERVAL):
        """
        Background loop: evaluate all domains periodically.
        """
        logger.info("StrategySelector: Starting background loop (interval=%.0fs)", interval)
        while True:
            try:
                count = await self.evaluate_all_domains()
                logger.debug("StrategySelector: Evaluated %d domains", count)
            except Exception as e:
                logger.error("StrategySelector: Loop error: %s", e)
            await asyncio.sleep(interval)

    async def get_domain_strategy(self, domain: str) -> str:
        """
        Get the recommended strategy for a domain.
        Falls back to 'http' if no profile exists.
        """
        try:
            profile = await self.repo.get_or_create_profile(domain)
            return profile.preferred_strategy
        except Exception:
            return "http"

    async def _get_domains_with_observations(self, min_count: int) -> Set[str]:
        """Get distinct domains that have enough observations."""
        obs = await self.repo.get_observations(limit=5000)
        domain_counts = {}
        for o in obs:
            domain_counts[o.domain] = domain_counts.get(o.domain, 0) + 1
        return {d for d, c in domain_counts.items() if c >= min_count}
