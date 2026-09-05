"""
The evaluator writes DomainProfile's two learned axes independently.

StrategyObservation.strategy mixes two vocabularies -- how the bytes were
fetched ("http", "browser") and how they were parsed ("overlay", "json_ld",
"semantic_html"). They used to be scored in one contest whose winner was
written to a single `preferred_strategy` field that RenderingPolicy read as a
fetch tier. StrategySelector.run_forever runs this hourly against every
domain (main.py), so both failure modes below were live.
"""

import pytest

from src.application.evaluator import StrategyEvaluator
from src.domain.models import DomainProfile, StrategyObservation


class FakeObsRepo:
    def __init__(self, profile: DomainProfile, observations: list[StrategyObservation]):
        self.profile = profile
        self.observations = observations
        self.updated: list[DomainProfile] = []

    async def get_observations(self, domain=None, strategy=None, limit=100, offset=0):
        return self.observations

    async def get_or_create_profile(self, domain: str) -> DomainProfile:
        return self.profile

    async def update_profile(self, profile: DomainProfile) -> None:
        self.updated.append(profile)


def _observations(strategy: str, count: int, *, success: bool = True) -> list[StrategyObservation]:
    return [
        StrategyObservation(
            observation_id=f"obs-{strategy}-{i}",
            job_id=f"job-{i}",
            domain="example.com",
            strategy=strategy,
            success=success,
            valid_record_count=1 if success else 0,
            required_field_completeness=1.0 if success else 0.0,
            latency_ms=100.0,
        )
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_extraction_winner_does_not_overwrite_the_fetch_tier():
    """A domain demoted to browser, with json_ld winning on extraction.

    The old single-field version wrote "json_ld" into preferred_strategy,
    where should_attempt_http_tier read it as "not browser" and sent the next
    fetch back down the tier-1 path the demotion existed to avoid.
    """
    profile = DomainProfile(domain="example.com", preferred_fetch_tier="browser")
    repo = FakeObsRepo(profile, _observations("json_ld", 6))

    updated = await StrategyEvaluator(repo=repo).update_domain_profile("example.com")

    assert updated.preferred_fetch_tier == "browser"
    assert updated.preferred_extraction_strategy == "json_ld"


@pytest.mark.asyncio
async def test_no_fetch_tier_evidence_leaves_the_demotion_alone():
    """No fetch-tier observations at all must mean "don't touch that axis".

    The old version seeded best_strategy = "http" and always wrote it, so an
    hourly pass reset every browser-only domain back to http -- and a
    freshly demoted domain almost never has five browser observations yet.
    """
    profile = DomainProfile(domain="example.com", preferred_fetch_tier="browser")
    repo = FakeObsRepo(profile, _observations("overlay", 6))

    updated = await StrategyEvaluator(repo=repo).update_domain_profile("example.com")

    assert updated.preferred_fetch_tier == "browser"


@pytest.mark.asyncio
async def test_fetch_tier_is_still_learned_when_there_is_evidence():
    """The guard above must not turn the fetch axis read-only."""
    profile = DomainProfile(domain="example.com")
    repo = FakeObsRepo(
        profile,
        _observations("http", 6, success=False) + _observations("browser", 6),
    )

    updated = await StrategyEvaluator(repo=repo).update_domain_profile("example.com")

    assert updated.preferred_fetch_tier == "browser"
    assert updated.preferred_extraction_strategy is None
