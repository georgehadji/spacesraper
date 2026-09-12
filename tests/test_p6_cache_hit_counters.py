"""P6 — the crawl cache's hit counters (D17).

_store_cache_entry writes the entry with setex, so the key holds a *string*.
_increment_cache_hit then called hincrby on that same key — a hash command
against a string value — which is a WRONGTYPE error on every single call,
swallowed by a bare `except Exception: pass`. The counters have always been
zero.

tests/test_cache.py concealed it: three cases set
`crawler._increment_cache_hit = AsyncMock()`, so the suite asserted the
counter was *called* and never that it *worked*. Those mocks are removed in
the same change — fixing the code while leaving them would make the next
regression equally invisible.

Scope: fakeredis, not a live Valkey. That is what makes these guards work at
all: fakeredis enforces Redis type semantics, so hincrby-on-a-string raises
there exactly as it does in production. A hand-rolled AsyncMock would happily
accept it, which is precisely how this survived.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.smart_crawler import CrawlCacheEntry, SmartCrawler

URL = "https://counters.example.invalid/a"


def fake_valkey():
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.FakeAsyncValkey(decode_responses=True)


async def crawler_with_entry(**entry_kwargs) -> SmartCrawler:
    crawler = SmartCrawler()
    crawler._valkey = fake_valkey()
    entry = CrawlCacheEntry(
        url=URL,
        content_hash="abc",
        cached_at=datetime.now(tz=UTC),
        **entry_kwargs,
    )
    await crawler._store_cache_entry(URL, entry)
    return crawler


@pytest.mark.asyncio
async def test_a_a_cache_hit_actually_increments_the_counter():
    """The guard for D17. The counters must survive a round trip."""
    crawler = await crawler_with_entry()

    await crawler._increment_cache_hit(URL)
    await crawler._increment_cache_hit(URL)

    stored = await crawler._get_cached_metadata(URL)
    assert stored is not None, "the entry itself was destroyed by the counter update"
    assert stored.hit_count == 2
    assert stored.access_count == 2


@pytest.mark.asyncio
async def test_b_incrementing_preserves_the_rest_of_the_entry():
    """Control: the counter update must not overwrite the cached metadata."""
    crawler = await crawler_with_entry(etag='"v1"', last_modified="Wed, 21 Oct 2026 07:28:00 GMT")

    await crawler._increment_cache_hit(URL)

    stored = await crawler._get_cached_metadata(URL)
    assert stored.etag == '"v1"'
    assert stored.last_modified == "Wed, 21 Oct 2026 07:28:00 GMT"
    assert stored.content_hash == "abc"


@pytest.mark.asyncio
async def test_c_incrementing_an_absent_entry_is_a_no_op():
    """Control: a hit on an evicted entry must not resurrect a stub."""
    crawler = SmartCrawler()
    crawler._valkey = fake_valkey()

    await crawler._increment_cache_hit(URL)

    assert await crawler._get_cached_metadata(URL) is None


@pytest.mark.asyncio
async def test_d_a_fresh_cache_hit_through_check_cache_counts(monkeypatch):
    """End to end through the real entry point, not the private method.

    This is the shape tests/test_cache.py had with _increment_cache_hit
    mocked out: check_cache on a fresh entry short-circuits and records a
    hit. With the mock gone it also asserts the hit was recorded.
    """
    crawler = await crawler_with_entry()

    result = await crawler.check_cache(URL)

    assert result.should_scrape is False
    assert result.cache_hit is True
    stored = await crawler._get_cached_metadata(URL)
    assert stored.hit_count == 1


@pytest.mark.asyncio
async def test_e_a_304_records_a_hit_and_keeps_the_entry(monkeypatch):
    """The other real caller: a stale entry revalidated with 304."""
    crawler = SmartCrawler()
    crawler._valkey = fake_valkey()
    await crawler._store_cache_entry(
        URL,
        CrawlCacheEntry(
            url=URL,
            content_hash="abc",
            etag='"v1"',
            cached_at=datetime.now(tz=UTC) - timedelta(hours=48),
        ),
    )

    class _Response:
        status_code = 304
        headers: dict = {}

    async def fake_head(url, **kwargs):
        return _Response()

    monkeypatch.setattr("src.smart_crawler.target_http.head", fake_head)

    result = await crawler.check_cache(URL)

    assert result.should_scrape is False
    assert result.reason == "304 Not Modified"
    stored = await crawler._get_cached_metadata(URL)
    assert stored.hit_count == 1
    assert stored.etag == '"v1"', "revalidation must not drop the validator"
