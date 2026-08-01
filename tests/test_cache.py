# Tests for smart_crawler cache behavior.
# Covers: cache miss, 304, unchanged ETag, force refresh, update after fetch.

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.smart_crawler import SmartCrawler, should_scrape_url, update_url_cache


@pytest.mark.asyncio
async def test_force_refresh_always_scrapes():
    """force_refresh=True causes should_scrape=True regardless of cache."""
    crawler = SmartCrawler()
    result = await crawler.check_cache("https://example.com", force_refresh=True)
    assert result.should_scrape is True
    assert "Force refresh" in result.reason


@pytest.mark.asyncio
async def test_cache_miss_scrapes():
    """No cache entry causes should_scrape=True."""
    crawler = SmartCrawler()
    result = await crawler.check_cache("https://unknown.com")
    assert result.should_scrape is True
    assert "No cache" in result.reason


@pytest.mark.asyncio
async def test_fresh_cache_skips_scrape():
    """A recent cache entry causes should_scrape=False."""
    crawler = SmartCrawler()
    from datetime import datetime, timezone, timedelta
    from src.smart_crawler import CrawlCacheEntry

    # Inject a fresh cache entry directly
    crawler._redis = AsyncMock()
    cache_entry = CrawlCacheEntry(
        url="https://fresh.com",
        content_hash="abc",
        cached_at=datetime.now(tz=timezone.utc),
    )
    crawler._get_cached_metadata = AsyncMock(return_value=cache_entry)
    crawler._increment_cache_hit = AsyncMock()

    result = await crawler.check_cache("https://fresh.com")
    assert result.should_scrape is False
    assert result.cache_hit is True


@pytest.mark.asyncio
async def test_stale_cache_with_304_skips_scrape():
    """Stale cache + 304 response causes should_scrape=False."""
    crawler = SmartCrawler()
    from datetime import datetime, timezone, timedelta
    from src.smart_crawler import CrawlCacheEntry

    crawler._redis = AsyncMock()
    stale_entry = CrawlCacheEntry(
        url="https://stale.com",
        content_hash="old",
        etag='"abc123"',
        cached_at=datetime.now(tz=timezone.utc) - timedelta(hours=48),
    )
    crawler._get_cached_metadata = AsyncMock(return_value=stale_entry)
    crawler._increment_cache_hit = AsyncMock()
    crawler._update_cache_timestamp = AsyncMock()

    # Mock the HTTP response to return 304
    mock_response = AsyncMock()
    mock_response.status_code = 304
    mock_response.headers = {}

    with patch.object(crawler, "_redis", create=True):
        with patch("src.smart_crawler.http_client") as mock_http:
            mock_http.head = AsyncMock(return_value=mock_response)
            result = await crawler.check_cache("https://stale.com")

    assert result.should_scrape is False
    assert result.cache_hit is True
    assert result.reason == "304 Not Modified"


@pytest.mark.asyncio
async def test_stale_cache_with_200_and_same_etag_skips_scrape():
    """Stale cache + 200 with matching ETag causes should_scrape=False."""
    crawler = SmartCrawler()
    from datetime import datetime, timezone, timedelta
    from src.smart_crawler import CrawlCacheEntry

    crawler._redis = AsyncMock()
    entry = CrawlCacheEntry(
        url="https://etag-match.com",
        content_hash="old",
        etag='"same"',
        cached_at=datetime.now(tz=timezone.utc) - timedelta(hours=48),
    )
    crawler._get_cached_metadata = AsyncMock(return_value=entry)
    crawler._increment_cache_hit = AsyncMock()

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"etag": '"same"'}

    with patch("src.smart_crawler.http_client") as mock_http:
        mock_http.head = AsyncMock(return_value=mock_response)
        result = await crawler.check_cache("https://etag-match.com")

    assert result.should_scrape is False
    assert result.cache_hit is True


@pytest.mark.asyncio
async def test_stale_cache_with_new_content_scrapes():
    """Stale cache + 200 with different ETag causes should_scrape=True."""
    crawler = SmartCrawler()
    from datetime import datetime, timezone, timedelta
    from src.smart_crawler import CrawlCacheEntry

    crawler._redis = AsyncMock()
    entry = CrawlCacheEntry(
        url="https://changed.com",
        content_hash="old",
        etag='"old"',
        cached_at=datetime.now(tz=timezone.utc) - timedelta(hours=48),
    )
    crawler._get_cached_metadata = AsyncMock(return_value=entry)

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"etag": '"new"', "last-modified": "now"}

    with patch("src.smart_crawler.http_client") as mock_http:
        mock_http.head = AsyncMock(return_value=mock_response)
        result = await crawler.check_cache("https://changed.com")

    assert result.should_scrape is True
    assert result.reason == "Content changed"


@pytest.mark.asyncio
async def test_update_url_cache_stores_entry():
    """update_url_cache stores a cache entry and returns a content hash."""
    crawler = SmartCrawler()
    crawler._redis = AsyncMock()
    crawler._store_cache_entry = AsyncMock()

    html = "<html>Hello</html>"
    content_hash = await update_url_cache("https://cached.com", html)
    assert content_hash is not None
    assert len(content_hash) == 64  # SHA256 hex


@pytest.mark.asyncio
async def test_should_scrape_url_convenience():
    """should_scrape_url returns (bool, hash) tuple."""
    crawler = SmartCrawler()
    crawler.check_cache = AsyncMock()
    crawler.check_cache.return_value = type('obj', (object,), {
        'should_scrape': True,
        'cache_hit': False,
        'cached_hash': None,
        'reason': 'test'
    })()

    # Patch the global smart_crawler
    with patch("src.smart_crawler.metrics_tracker") as mock_metrics:
        mock_metrics.increment = AsyncMock()
        with patch("src.smart_crawler.smart_crawler", crawler):
            result_should, result_hash = await should_scrape_url("https://test.com")
            assert result_should is True


@pytest.mark.asyncio
async def test_cache_error_falls_back_to_scrape():
    """Cache check error causes should_scrape=True (fail open)."""
    crawler = SmartCrawler()
    from datetime import datetime, timezone, timedelta
    from src.smart_crawler import CrawlCacheEntry

    crawler._redis = AsyncMock()
    entry = CrawlCacheEntry(
        url="https://error.com",
        content_hash="old",
        etag='"old"',
        cached_at=datetime.now(tz=timezone.utc) - timedelta(hours=48),
    )
    crawler._get_cached_metadata = AsyncMock(return_value=entry)

    with patch("src.smart_crawler.http_client.head", side_effect=Exception("Network error")):
        result = await crawler.check_cache("https://error.com")

    assert result.should_scrape is True
    assert "error" in result.reason.lower()
