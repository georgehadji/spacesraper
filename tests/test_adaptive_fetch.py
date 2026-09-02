# P1: AdaptiveFetchService + RenderingPolicy contract tests.
# docs/plans/2026-08-13-capability-enhancement-plan.md P1 deliverable:
# "blocked-HTTP fixture escalates; plain-HTML fixture never launches a browser."

import pytest

from src.application.adaptive_fetch import AdaptiveFetchService
from src.application.rendering_policy import should_attempt_http_tier
from src.domain.fetch import FetchResult
from src.domain.models import DomainProfile


class FakeHttpFetcher:
    """A FetcherPort stand-in that never touches curl_cffi or the network."""

    def __init__(self, result: FetchResult):
        self._result = result
        self.calls = 0

    async def fetch(self, request):
        self.calls += 1
        return self._result


class FakeObsRepo:
    def __init__(self):
        self.updated_profiles: list[DomainProfile] = []
        self.observations: list = []

    async def update_profile(self, profile: DomainProfile) -> None:
        self.updated_profiles.append(profile)

    async def create_observation(self, obs) -> None:
        self.observations.append(obs)


def test_should_attempt_http_tier_true_for_fresh_domain():
    assert should_attempt_http_tier(DomainProfile(domain="example.com")) is True


def test_should_attempt_http_tier_false_once_demoted_to_browser():
    profile = DomainProfile(domain="example.com", preferred_strategy="browser")
    assert should_attempt_http_tier(profile) is False


def test_should_attempt_http_tier_false_above_block_rate_threshold():
    profile = DomainProfile(domain="example.com", block_rate=0.5)
    assert should_attempt_http_tier(profile) is False


@pytest.mark.asyncio
async def test_plain_html_fixture_never_launches_a_browser():
    """A clean Tier-1 hit returns the FetchResult directly — the caller never
    needs to construct a browser fetcher at all."""
    clean = FetchResult(url="https://example.com", status_code=200, html="<html>ok</html>", tier_used="http")
    fetcher = FakeHttpFetcher(clean)
    obs_repo = FakeObsRepo()
    service = AdaptiveFetchService(http_fetcher=fetcher, obs_repo=obs_repo)

    result = await service.try_tier1("https://example.com", "example.com", DomainProfile(domain="example.com"))

    assert result is clean
    assert fetcher.calls == 1
    assert obs_repo.updated_profiles == []  # no demotion on a clean hit


@pytest.mark.asyncio
async def test_blocked_http_fixture_escalates_and_persists_demotion():
    """A blocked Tier-1 response returns None (escalate to the browser) and
    persists preferred_strategy="browser" so the next fetch for this domain
    skips Tier-1 entirely."""
    blocked = FetchResult(
        url="https://example.com", status_code=403, html="<html>captcha</html>",
        tier_used="http", blocked=True, block_reason="status_403",
    )
    fetcher = FakeHttpFetcher(blocked)
    obs_repo = FakeObsRepo()
    service = AdaptiveFetchService(http_fetcher=fetcher, obs_repo=obs_repo)
    profile = DomainProfile(domain="example.com")

    result = await service.try_tier1("https://example.com", "example.com", profile)

    assert result is None
    assert len(obs_repo.updated_profiles) == 1
    assert obs_repo.updated_profiles[0].preferred_strategy == "browser"


@pytest.mark.asyncio
async def test_try_tier1_skips_fetch_entirely_when_policy_says_no():
    fetcher = FakeHttpFetcher(FetchResult(url="x", tier_used="http"))
    service = AdaptiveFetchService(http_fetcher=fetcher, obs_repo=FakeObsRepo())
    profile = DomainProfile(domain="example.com", preferred_strategy="browser")

    result = await service.try_tier1("https://example.com", "example.com", profile)

    assert result is None
    assert fetcher.calls == 0
