# P1: owns the Tier-1 (cheap HTTP) attempt-and-escalate decision. Tier 2
# (browser) execution stays in worker_scraper.py's existing ScraperEngine
# path — see browser_fetcher.py's docstring for why.
# docs/plans/2026-08-13-capability-enhancement-plan.md P1.

import logging
import uuid

from src.application.rendering_policy import should_attempt_http_tier
from src.domain.fetch import FetchRequest, FetchResult
from src.domain.models import DomainProfile, StrategyObservation
from src.domain.ports import FetcherPort, ObservationRepository

logger = logging.getLogger("Spacescraper.AdaptiveFetch")


class AdaptiveFetchService:
    def __init__(self, http_fetcher: FetcherPort, obs_repo: ObservationRepository):
        self.http_fetcher = http_fetcher
        self.obs_repo = obs_repo

    async def try_tier1(self, url: str, domain: str, profile: DomainProfile) -> FetchResult | None:
        """Returns a usable FetchResult on a clean Tier-1 hit, or None — a
        miss (policy skip, block, or transport failure) means "fall through
        to the browser in this same job," never a raised exception."""
        if not should_attempt_http_tier(profile):
            return None

        result = await self.http_fetcher.fetch(FetchRequest(url=url))
        if not result.ok:
            await self._demote(domain, profile)
            return None
        return result

    async def _demote(self, domain: str, profile: DomainProfile) -> None:
        if profile.preferred_strategy == "browser":
            return
        try:
            await self.obs_repo.update_profile(
                profile.model_copy(update={"preferred_strategy": "browser"})
            )
        except Exception:
            logger.debug("Failed to persist tier-1 demotion for %s", domain, exc_info=True)

    async def record_observation(
        self, job_id: str, domain: str, strategy: str, *,
        success: bool, latency_ms: float, blocked: bool = False,
    ) -> None:
        obs = StrategyObservation(
            observation_id=f"obs_{uuid.uuid4().hex[:12]}",
            job_id=job_id, domain=domain, strategy=strategy,
            success=success, blocked=blocked, latency_ms=latency_ms,
            valid_record_count=1 if success else 0,
            required_field_completeness=1.0 if success else 0.0,
        )
        try:
            await self.obs_repo.create_observation(obs)
        except Exception:
            logger.debug("Failed to record tier observation for %s", domain, exc_info=True)
