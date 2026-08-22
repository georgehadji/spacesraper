# P2: SitemapSeeder — robots.txt Sitemap: discovery, index recursion.
# docs/plans/2026-08-13-capability-enhancement-plan.md P2.

from unittest.mock import AsyncMock

import pytest

from src.application.sitemap_seeder import discover_sitemap_urls

URLSET_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://example.com/a</loc></url>
<url><loc>https://example.com/b</loc></url>
</urlset>'''

SITEMAP_INDEX_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://example.com/sitemap-1.xml</loc></sitemap>
</sitemapindex>'''


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


@pytest.mark.asyncio
async def test_discovers_via_robots_txt_sitemap_directive(monkeypatch):
    async def fake_get(url, **kwargs):
        if url.endswith("/robots.txt"):
            return _FakeResponse(200, "User-agent: *\nSitemap: https://example.com/custom-sitemap.xml\n")
        if url == "https://example.com/custom-sitemap.xml":
            return _FakeResponse(200, URLSET_XML)
        return _FakeResponse(404)

    monkeypatch.setattr("src.application.sitemap_seeder.target_http.get", AsyncMock(side_effect=fake_get))

    urls = await discover_sitemap_urls("https://example.com/")
    assert urls == ["https://example.com/a", "https://example.com/b"]


@pytest.mark.asyncio
async def test_falls_back_to_well_known_path_when_robots_has_no_sitemap(monkeypatch):
    async def fake_get(url, **kwargs):
        if url.endswith("/robots.txt"):
            return _FakeResponse(200, "User-agent: *\nDisallow: /private/\n")
        if url == "https://example.com/sitemap.xml":
            return _FakeResponse(200, URLSET_XML)
        return _FakeResponse(404)

    monkeypatch.setattr("src.application.sitemap_seeder.target_http.get", AsyncMock(side_effect=fake_get))

    urls = await discover_sitemap_urls("https://example.com/")
    assert urls == ["https://example.com/a", "https://example.com/b"]


@pytest.mark.asyncio
async def test_recurses_one_level_into_sitemap_index(monkeypatch):
    async def fake_get(url, **kwargs):
        if url.endswith("/robots.txt"):
            return _FakeResponse(200, "Sitemap: https://example.com/sitemap-index.xml\n")
        if url == "https://example.com/sitemap-index.xml":
            return _FakeResponse(200, SITEMAP_INDEX_XML)
        if url == "https://example.com/sitemap-1.xml":
            return _FakeResponse(200, URLSET_XML)
        return _FakeResponse(404)

    monkeypatch.setattr("src.application.sitemap_seeder.target_http.get", AsyncMock(side_effect=fake_get))

    urls = await discover_sitemap_urls("https://example.com/")
    assert urls == ["https://example.com/a", "https://example.com/b"]


@pytest.mark.asyncio
async def test_no_sitemap_anywhere_returns_empty(monkeypatch):
    monkeypatch.setattr(
        "src.application.sitemap_seeder.target_http.get",
        AsyncMock(return_value=_FakeResponse(404)),
    )
    assert await discover_sitemap_urls("https://example.com/") == []


@pytest.mark.asyncio
async def test_result_bounded_by_max_urls(monkeypatch):
    many_urls_xml = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<url><loc>https://example.com/{i}</loc></url>" for i in range(10))
        + "</urlset>"
    )

    async def fake_get(url, **kwargs):
        if url.endswith("/robots.txt"):
            return _FakeResponse(404)
        if url == "https://example.com/sitemap.xml":
            return _FakeResponse(200, many_urls_xml)
        return _FakeResponse(404)

    monkeypatch.setattr("src.application.sitemap_seeder.target_http.get", AsyncMock(side_effect=fake_get))

    urls = await discover_sitemap_urls("https://example.com/", max_urls=3)
    assert len(urls) == 3
