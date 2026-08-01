import pytest
from src.extractors.strategies.override import OverrideStrategy

SAMPLE_HTML = """<!DOCTYPE html>
<html><body>
  <article class="product">
    <h2 class="title">Widget Pro</h2>
    <span class="price">$29.99</span>
    <a class="link" href="/widget-pro">Details</a>
  </article>
  <article class="product">
    <h2 class="title">Widget Lite</h2>
    <span class="price">$14.99</span>
    <a class="link" href="/widget-lite">Details</a>
  </article>
</body></html>"""


@pytest.mark.asyncio
async def test_override_extracts_from_mapped_selectors():
    strategy = OverrideStrategy()
    overlay = {
        "mappings": {"title": ".title", "price": ".price"},
        "container_selector": "article.product",
    }
    result = await strategy.extract(SAMPLE_HTML, [], "https://shop.example.com", overlay)
    assert len(result) == 2
    assert result[0].data["title"] == "Widget Pro"
    assert result[0].data["price"] == "$29.99"


@pytest.mark.asyncio
async def test_override_resolves_img_src():
    html = '<div class="card"><img class="photo" src="/img/1.jpg" /></div>'
    strategy = OverrideStrategy()
    overlay = {"mappings": {"photo_url": ".photo"}, "container_selector": ".card"}
    result = await strategy.extract(html, [], "https://x.com", overlay)
    assert len(result) == 1
    assert result[0].data["photo_url"] == "/img/1.jpg"


@pytest.mark.asyncio
async def test_override_resolves_link_href():
    html = '<div class="card"><a class="site" href="https://example.com">Example</a></div>'
    strategy = OverrideStrategy()
    overlay = {"mappings": {"site": ".site"}, "container_selector": ".card"}
    result = await strategy.extract(html, [], "https://x.com", overlay)
    assert len(result) == 1
    assert result[0].data["site"] == "https://example.com"


@pytest.mark.asyncio
async def test_override_skips_empty_containers():
    html = '<div class="card">No data here</div>'
    strategy = OverrideStrategy()
    overlay = {"mappings": {"title": ".nonexistent"}, "container_selector": ".card"}
    result = await strategy.extract(html, [], "https://x.com", overlay)
    assert len(result) == 0


@pytest.mark.asyncio
async def test_override_no_mappings_no_results():
    html = '<div class="card"><span class="title">X</span></div>'
    strategy = OverrideStrategy()
    overlay: dict = {}
    result = await strategy.extract(html, [], "https://x.com", overlay)
    assert len(result) == 0


@pytest.mark.asyncio
async def test_override_records_have_identity_hash():
    html = '<div class="card"><span class="title">X</span></div>'
    strategy = OverrideStrategy()
    overlay = {"mappings": {"title": ".title"}, "container_selector": ".card"}
    result = await strategy.extract(html, [], "https://x.com", overlay)
    assert len(result) == 1
    assert result[0].identity_hash is not None
    assert len(result[0].identity_hash) > 0
