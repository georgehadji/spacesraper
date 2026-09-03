"""Tests for the OpenRouter web-search Discovery adapter.

Two properties get the most attention here, because both fail silently and
expensively rather than loudly:

* the fan-out cap, since every request is separately billed; and
* the allowlist, which this adapter must not be able to bypass — it returns
  raw hits and DiscoveryService is what decides which ones survive.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.application.discovery_service import DiscoveryService
from src.domain.exceptions import DiscoveryRefusedError
from src.domain.models import ResearchPlan, SearchHit
from src.infrastructure.ai.ssot import (
    WEB_SEARCH_MAX_RESULTS_CAP,
    WEB_SEARCH_TOOL_TYPE,
    AIJob,
    profile_for,
)
from src.infrastructure.providers.search_provider import (
    OpenRouterSearchProvider,
    SearchProvider,
    _parse_url_citations,
)
from src.security.url_policy import UrlPolicy

API_KEY = "test-key-not-a-real-credential"


def _annotation(url: str, title: str = "T", content: str = "S") -> dict:
    return {"type": "url_citation", "url_citation": {"url": url, "title": title, "content": content}}


def _response(annotations: list[dict], text: str = "") -> dict:
    return {"choices": [{"message": {"content": text, "annotations": annotations}}]}


def _provider(*, max_fanout: int | None = None, cache=None) -> OpenRouterSearchProvider:
    cache = cache or MagicMock(get=AsyncMock(return_value=None), set=AsyncMock())
    return OpenRouterSearchProvider(api_key=API_KEY, cache=cache, max_fanout=max_fanout)


def _client_returning(payload: dict) -> MagicMock:
    response = MagicMock()
    response.json = MagicMock(return_value=payload)
    return MagicMock(post=AsyncMock(return_value=response))


# --- port conformance -----------------------------------------------------


def test_is_a_search_provider():
    assert isinstance(_provider(), SearchProvider)


@pytest.mark.asyncio
async def test_disabled_without_key_returns_empty_and_bills_nothing():
    provider = OpenRouterSearchProvider(api_key=None)
    assert await provider.is_available() is False
    client = _client_returning(_response([]))
    with patch.object(provider, "_get_client", AsyncMock(return_value=client)):
        assert await provider.search("q") == []
    client.post.assert_not_called()


# --- fan-out cap (cost) ---------------------------------------------------


@pytest.mark.asyncio
async def test_request_is_capped_to_fanout_budget_before_billing():
    """A billed request must not ask for more hits than Discovery can use."""
    provider = _provider(max_fanout=3)
    client = _client_returning(_response([_annotation(f"https://e.com/{i}") for i in range(3)]))

    with patch.object(provider, "_get_client", AsyncMock(return_value=client)):
        await provider.search("q", max_results=50)

    sent = client.post.call_args.kwargs["json"]
    tool = sent["tools"][0]
    assert tool["type"] == WEB_SEARCH_TOOL_TYPE
    assert tool["parameters"]["max_results"] == 3


@pytest.mark.asyncio
async def test_request_is_capped_by_absolute_ceiling_without_a_fanout_budget():
    provider = _provider(max_fanout=None)
    client = _client_returning(_response([]))

    with patch.object(provider, "_get_client", AsyncMock(return_value=client)):
        await provider.search("q", max_results=500)

    sent = client.post.call_args.kwargs["json"]
    assert sent["tools"][0]["parameters"]["max_results"] == WEB_SEARCH_MAX_RESULTS_CAP


@pytest.mark.asyncio
async def test_small_request_is_not_inflated_to_the_cap():
    provider = _provider(max_fanout=25)
    client = _client_returning(_response([]))

    with patch.object(provider, "_get_client", AsyncMock(return_value=client)):
        await provider.search("q", max_results=2)

    assert client.post.call_args.kwargs["json"]["tools"][0]["parameters"]["max_results"] == 2


@pytest.mark.asyncio
async def test_results_are_truncated_to_the_effective_cap():
    """Even if the API over-delivers, we return no more than we budgeted for."""
    provider = _provider(max_fanout=2)
    over = _response([_annotation(f"https://e.com/{i}") for i in range(9)])
    client = _client_returning(over)

    with patch.object(provider, "_get_client", AsyncMock(return_value=client)):
        hits = await provider.search("q", max_results=9)

    assert len(hits) == 2


@pytest.mark.asyncio
async def test_cached_query_does_not_issue_a_second_billed_request():
    cached = [SearchHit(url="https://e.com/a", title="T", snippet="S", rank=0, provider="openrouter").model_dump()]
    cache = MagicMock(get=AsyncMock(return_value=cached), set=AsyncMock())
    provider = _provider(cache=cache)
    client = _client_returning(_response([]))

    with patch.object(provider, "_get_client", AsyncMock(return_value=client)):
        hits = await provider.search("q")

    client.post.assert_not_called()
    assert [h.url for h in hits] == ["https://e.com/a"]


# --- trust: only real citations count -------------------------------------


def test_prose_urls_are_ignored():
    """A URL in the reply text is a generated token and may not exist.

    Trusting it would let a model inject an arbitrary target into the crawl
    queue, so only url_citation annotations are read.
    """
    payload = _response(
        [_annotation("https://real.example/page")],
        text="You should also visit https://hallucinated.example/evil for more.",
    )
    hits = _parse_url_citations(payload, max_results=10)
    assert [h.url for h in hits] == ["https://real.example/page"]


def test_non_citation_annotations_are_ignored():
    payload = _response([{"type": "file_citation", "url": "https://nope.example"}])
    assert _parse_url_citations(payload, max_results=10) == []


def test_duplicate_urls_are_collapsed():
    payload = _response([_annotation("https://e.com/a"), _annotation("https://e.com/a")])
    assert len(_parse_url_citations(payload, max_results=10)) == 1


def test_flat_citation_shape_is_tolerated():
    """Guard against silently returning nothing if the payload shape changes."""
    payload = _response([{"type": "url_citation", "url": "https://e.com/a", "title": "T"}])
    assert [h.url for h in _parse_url_citations(payload, max_results=10)] == ["https://e.com/a"]


def test_malformed_payload_yields_no_hits():
    for payload in ({}, {"choices": []}, {"choices": [{}]}, {"choices": [{"message": {}}]}):
        assert _parse_url_citations(payload, max_results=5) == []


@pytest.mark.asyncio
async def test_ranks_are_sequential_from_zero():
    provider = _provider()
    client = _client_returning(_response([_annotation(f"https://e.com/{i}") for i in range(3)]))

    with patch.object(provider, "_get_client", AsyncMock(return_value=client)):
        hits = await provider.search("q")

    assert [h.rank for h in hits] == [0, 1, 2]
    assert {h.provider for h in hits} == {"openrouter"}


# --- resilience: the port promises never to raise -------------------------


@pytest.mark.asyncio
async def test_transport_failure_returns_empty_rather_than_raising():
    provider = _provider()
    client = MagicMock(post=AsyncMock(side_effect=RuntimeError("connection reset")))

    with patch.object(provider, "_get_client", AsyncMock(return_value=client)), \
         patch("asyncio.sleep", AsyncMock()):
        assert await provider.search("q") == []


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold_and_stops_billing():
    from src.infrastructure.ai.ssot import RESILIENCE

    provider = _provider()
    client = MagicMock(post=AsyncMock(side_effect=RuntimeError("down")))

    with patch.object(provider, "_get_client", AsyncMock(return_value=client)), \
         patch("asyncio.sleep", AsyncMock()):
        for _ in range(RESILIENCE.breaker_threshold):
            await provider.search("q")

    assert await provider.is_available() is False
    calls_before = client.post.await_count
    with patch.object(provider, "_get_client", AsyncMock(return_value=client)):
        assert await provider.search("q") == []
    assert client.post.await_count == calls_before, "open breaker must not bill"


# --- the adapter must not be able to bypass the allowlist -----------------


def _plan(**kw) -> ResearchPlan:
    defaults = dict(plan_id="p1", query="tenders", max_results=10,
                    allowed_domains=["example.com"])
    defaults.update(kw)
    return ResearchPlan(**defaults)


@pytest.fixture
def no_dns():
    """Neutralise the SSRF guard's hostname resolution.

    The guard resolves every candidate URL, so without this these tests fail
    whenever DNS is slow or unavailable: the hits are counted as ssrf_blocked
    and the assertions about allowlist and fan-out behaviour never get to run.
    The guard has its own dedicated tests; here it is noise.
    """
    with patch("src.application.discovery_service.validate_outbound_url", MagicMock()):
        yield


@pytest.mark.asyncio
async def test_offdomain_hits_are_rejected_by_discovery_service(no_dns):
    """The provider returns hits; UrlPolicy is what decides they may be used."""
    hits = [
        SearchHit(url="https://example.com/a", title="ok", snippet="", rank=0, provider="openrouter"),
        SearchHit(url="https://untrusted.com/b", title="bad", snippet="", rank=1, provider="openrouter"),
    ]
    provider = MagicMock(search=AsyncMock(return_value=hits))
    queue = MagicMock(get_allowed_fanout=AsyncMock(side_effect=lambda _p, n, _m: n))

    service = DiscoveryService(
        search_provider=provider,
        url_policy=UrlPolicy(allowlist=["example.com"], denylist=[], respect_robots=False),
        queue=queue,
        discovery_max_fanout=25,
    )
    jobs, rejections, _hits = await service.discover(_plan())

    assert [j.url for j in jobs] == ["https://example.com/a"]
    assert rejections["policy_denied"] == 1


@pytest.mark.asyncio
async def test_discovery_refuses_to_run_with_an_empty_allowlist():
    """Search must never target arbitrary hosts, whatever the provider is."""
    provider = MagicMock(search=AsyncMock(return_value=[]))
    service = DiscoveryService(
        search_provider=provider,
        url_policy=UrlPolicy(allowlist=[], denylist=[], respect_robots=False),
        queue=MagicMock(get_allowed_fanout=AsyncMock(return_value=0)),
        discovery_max_fanout=25,
    )

    with pytest.raises(DiscoveryRefusedError):
        await service.discover(_plan(allowed_domains=[]))

    provider.search.assert_not_called(), "must refuse before issuing a billed search"


@pytest.mark.asyncio
async def test_discovery_searches_exactly_once_per_plan(no_dns):
    """Regression guard: the worker used to re-search just to archive the SERP.

    On a metered provider that doubled the per-request spend, and because search
    results are not stable between calls it could archive a SERP that never
    produced the enqueued jobs.
    """
    hits = [SearchHit(url="https://example.com/a", title="ok", snippet="", rank=0, provider="openrouter")]
    provider = MagicMock(search=AsyncMock(return_value=hits))
    service = DiscoveryService(
        search_provider=provider,
        url_policy=UrlPolicy(allowlist=["example.com"], denylist=[], respect_robots=False),
        queue=MagicMock(get_allowed_fanout=AsyncMock(side_effect=lambda _p, n, _m: n)),
        discovery_max_fanout=25,
    )

    await service.discover(_plan())

    assert provider.search.await_count == 1


@pytest.mark.asyncio
async def test_discovery_returns_the_raw_hits_it_used(no_dns):
    """The SERP archived for replay must be the one the jobs were built from."""
    hits = [
        SearchHit(url="https://example.com/a", title="ok", snippet="", rank=0, provider="openrouter"),
        SearchHit(url="https://untrusted.com/b", title="bad", snippet="", rank=1, provider="openrouter"),
    ]
    provider = MagicMock(search=AsyncMock(return_value=hits))
    service = DiscoveryService(
        search_provider=provider,
        url_policy=UrlPolicy(allowlist=["example.com"], denylist=[], respect_robots=False),
        queue=MagicMock(get_allowed_fanout=AsyncMock(side_effect=lambda _p, n, _m: n)),
        discovery_max_fanout=25,
    )

    result = await service.discover(_plan())

    # Raw and unfiltered: the rejected hit is still part of the archived SERP,
    # which is what makes the artifact a faithful record of what search returned.
    assert result.hits == hits
    assert [j.url for j in result.jobs] == ["https://example.com/a"]


@pytest.mark.asyncio
async def test_discovery_fanout_budget_truncates_survivors(no_dns):
    hits = [
        SearchHit(url=f"https://example.com/{i}", title="ok", snippet="", rank=i, provider="openrouter")
        for i in range(6)
    ]
    provider = MagicMock(search=AsyncMock(return_value=hits))
    queue = MagicMock(get_allowed_fanout=AsyncMock(return_value=2))

    service = DiscoveryService(
        search_provider=provider,
        url_policy=UrlPolicy(allowlist=["example.com"], denylist=[], respect_robots=False),
        queue=queue,
        discovery_max_fanout=2,
    )
    jobs, rejections, _hits = await service.discover(_plan())

    assert len(jobs) == 2
    assert rejections["fanout_budget_exceeded"] == 4


# --- SSOT wiring ----------------------------------------------------------


def test_search_job_is_pinned_in_the_ssot():
    profile = profile_for(AIJob.SEARCH)
    assert profile.model.id
    assert profile.fallbacks, "search should survive a single-vendor outage"
    # Server-side search plus generation is slower than a plain completion.
    assert profile.timeout_s >= 30.0


@pytest.mark.asyncio
async def test_request_uses_the_server_tool_not_the_deprecated_forms():
    """`plugins: [{"id": "web"}]` and the `:online` suffix are both deprecated.

    Asserted against the payload actually sent rather than by grepping the
    source, so the adapter's docstring is free to name the deprecated forms in
    order to explain why they are avoided.
    """
    provider = _provider()
    client = _client_returning(_response([]))

    with patch.object(provider, "_get_client", AsyncMock(return_value=client)):
        await provider.search("q")

    sent = client.post.call_args.kwargs["json"]
    assert "plugins" not in sent, "deprecated plugins syntax is in use"
    assert sent["tools"][0]["type"] == WEB_SEARCH_TOOL_TYPE == "openrouter:web_search"
    for model_id in [sent["model"], *sent.get("models", [])]:
        assert not model_id.endswith(":online"), "deprecated :online suffix is in use"
