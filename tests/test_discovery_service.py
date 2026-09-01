"""
Task 3.3 — Tests for DiscoveryService.
Security-critical: a query returning a private-IP host must enqueue nothing;
a query with an empty allowlist must be refused; the fan-out cap must hold.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.application.discovery_service import DiscoveryService
from src.domain.models import ResearchPlan, SearchHit
from src.domain.exceptions import DiscoveryRefusedError
from src.infrastructure.providers.search_provider import NoOpSearchProvider
from src.security.url_policy import UrlPolicy


def make_plan(**overrides) -> ResearchPlan:
    defaults = dict(
        plan_id="rp_test1",
        query="test query",
        max_results=10,
        allowed_domains=["example.com"],
    )
    defaults.update(overrides)
    return ResearchPlan(**defaults)


class FakeSearchProvider:
    def __init__(self, hits):
        self._hits = hits

    async def search(self, query, *, max_results=10):
        return self._hits[:max_results]

    async def is_available(self):
        return True


class FakeQueue:
    """Fan-out budget stub: allows up to `cap` jobs, tracks calls."""

    def __init__(self, cap=25):
        self.cap = cap
        self.calls = []

    async def get_allowed_fanout(self, root_job_id, requested, max_fanout):
        self.calls.append((root_job_id, requested, max_fanout))
        return min(requested, self.cap, max_fanout)


@pytest.mark.asyncio
async def test_empty_allowlist_is_refused():
    """Discovery requires a non-empty allowlist; empty = refuse to run."""
    plan = make_plan(allowed_domains=[])
    service = DiscoveryService(
        search_provider=NoOpSearchProvider(),
        url_policy=UrlPolicy(),
        queue=FakeQueue(),
    )

    with pytest.raises(DiscoveryRefusedError):
        await service.discover(plan)


@pytest.mark.asyncio
async def test_private_ip_host_enqueues_nothing():
    """A query returning a private-IP host must enqueue nothing (SSRF)."""
    hits = [
        SearchHit(url="http://169.254.169.254/metadata", title="metadata", rank=0, provider="test"),
    ]
    plan = make_plan(allowed_domains=["169.254.169.254"])  # even if "allowed" by domain, SSRF still blocks
    service = DiscoveryService(
        search_provider=FakeSearchProvider(hits),
        url_policy=UrlPolicy(allowlist=["169.254.169.254"], respect_robots=False),
        queue=FakeQueue(),
    )

    jobs, rejections = await service.discover(plan)

    assert jobs == []
    assert rejections.get("ssrf_blocked") == 1


@pytest.mark.asyncio
async def test_domain_not_in_allowlist_is_rejected():
    hits = [
        SearchHit(url="https://untrusted.com/page", title="t", rank=0, provider="test"),
    ]
    plan = make_plan(allowed_domains=["example.com"])
    service = DiscoveryService(
        search_provider=FakeSearchProvider(hits),
        url_policy=UrlPolicy(allowlist=["example.com"], respect_robots=False),
        queue=FakeQueue(),
    )

    jobs, rejections = await service.discover(plan)

    assert jobs == []
    assert rejections.get("policy_denied") == 1


@pytest.mark.asyncio
async def test_allowed_public_url_produces_scrape_job():
    hits = [
        SearchHit(url="https://example.com/article", title="t", rank=0, provider="test"),
    ]
    plan = make_plan(allowed_domains=["example.com"])
    service = DiscoveryService(
        search_provider=FakeSearchProvider(hits),
        url_policy=UrlPolicy(allowlist=["example.com"], respect_robots=False),
        queue=FakeQueue(),
    )

    jobs, rejections = await service.discover(plan)

    assert len(jobs) == 1
    assert jobs[0].url == "https://example.com/article"
    assert not any(rejections.values())


@pytest.mark.asyncio
async def test_fanout_cap_holds():
    """Fan-out cap must hold even when search returns more hits than the cap."""
    hits = [
        SearchHit(url=f"https://example.com/page{i}", title=f"t{i}", rank=i, provider="test")
        for i in range(50)
    ]
    plan = make_plan(allowed_domains=["example.com"], max_results=50)
    queue = FakeQueue(cap=5)  # discovery cap much lower than crawl cap
    service = DiscoveryService(
        search_provider=FakeSearchProvider(hits),
        url_policy=UrlPolicy(allowlist=["example.com"], respect_robots=False),
        queue=queue,
        discovery_max_fanout=25,
    )

    jobs, rejections = await service.discover(plan)

    assert len(jobs) == 5
    assert rejections.get("fanout_budget_exceeded") == 45
    # Confirm the discovery cap (25) was passed, not the crawl cap (200)
    assert queue.calls[0][2] == 25


@pytest.mark.asyncio
async def test_dedup_by_canonical_url():
    hits = [
        SearchHit(url="https://example.com/a", title="t1", rank=0, provider="test"),
        SearchHit(url="https://example.com/a/", title="t1-dup", rank=1, provider="test"),  # trailing slash dup
        SearchHit(url="https://EXAMPLE.com/a", title="t1-case-dup", rank=2, provider="test"),  # case dup
        SearchHit(url="https://example.com/b", title="t2", rank=3, provider="test"),
    ]
    plan = make_plan(allowed_domains=["example.com"])
    service = DiscoveryService(
        search_provider=FakeSearchProvider(hits),
        url_policy=UrlPolicy(allowlist=["example.com"], respect_robots=False),
        queue=FakeQueue(),
    )

    jobs, rejections = await service.discover(plan)

    assert len(jobs) == 2  # /a and /b survive, dupes collapsed
    assert rejections.get("duplicate") == 2


@pytest.mark.asyncio
async def test_cache_fresh_hits_are_skipped():
    hits = [
        SearchHit(url="https://example.com/fresh", title="t", rank=0, provider="test"),
    ]
    plan = make_plan(allowed_domains=["example.com"])

    fake_crawler = MagicMock()
    cache_result = MagicMock()
    cache_result.should_scrape = False
    fake_crawler.check_cache = AsyncMock(return_value=cache_result)

    service = DiscoveryService(
        search_provider=FakeSearchProvider(hits),
        url_policy=UrlPolicy(allowlist=["example.com"], respect_robots=False),
        queue=FakeQueue(),
        smart_crawler=fake_crawler,
    )

    jobs, rejections = await service.discover(plan)

    assert jobs == []
    assert rejections.get("cache_fresh") == 1


@pytest.mark.asyncio
async def test_no_hits_produces_no_jobs_no_error():
    plan = make_plan(allowed_domains=["example.com"])
    service = DiscoveryService(
        search_provider=NoOpSearchProvider(),
        url_policy=UrlPolicy(allowlist=["example.com"], respect_robots=False),
        queue=FakeQueue(),
    )

    jobs, rejections = await service.discover(plan)

    assert jobs == []
    assert not any(rejections.values())
