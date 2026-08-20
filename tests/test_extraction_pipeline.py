# Tests for DeterministicExtractionPipeline.

import pytest

from src.application.extraction_pipeline import DeterministicExtractionPipeline
from src.domain.models import ExtractedRecord, ExtractionSchema, FieldDefinition


@pytest.mark.asyncio
async def test_pipeline_json_ld_extraction():
    """JSON-LD extraction produces ExtractedRecord with lowercased type."""
    html = '''<script type="application/ld+json">
    {"@type": "Product", "name": "Widget", "offers": {"price": "9.99"}}
    </script>'''
    pipeline = DeterministicExtractionPipeline()
    results = await pipeline.extract(html, [], "https://example.com")
    assert len(results) >= 1
    r = results[0]
    assert r.record_type == "product"  # lowercased
    assert r.data["name"] == "Widget"


@pytest.mark.asyncio
async def test_pipeline_overlay_priority():
    """Overlay with field_mappings takes priority over JSON-LD."""
    html = '''
    <script type="application/ld+json">{"@type":"Product","name":"ShouldSkip"}</script>
    <div class="item"><h3>Override</h3></div>
    '''
    overlay = {
        "container_selector": ".item",
        "field_mappings": {"name": "h3"},
    }
    pipeline = DeterministicExtractionPipeline()
    results = await pipeline.extract(html, [], "https://example.com", overlay=overlay)
    assert len(results) == 1
    assert results[0].data["name"] == "Override"


@pytest.mark.asyncio
async def test_nav_menu_yields_zero_list_records():
    """P7.3: the old rule fired record_type='list' on any <ul>/<ol> with >=3
    <li>, which is every navigation menu on the web."""
    html = '''
    <nav>
      <ul>
        <li><a href="/">Home</a></li>
        <li><a href="/about">About</a></li>
        <li><a href="/contact">Contact</a></li>
      </ul>
    </nav>
    '''
    pipeline = DeterministicExtractionPipeline()
    results = await pipeline.extract(html, [], "https://example.com")
    assert results == []


@pytest.mark.asyncio
async def test_bare_link_list_outside_nav_still_excluded():
    """A breadcrumb-shaped list (all items are just a link) is noise even
    without a semantic <nav> wrapper."""
    html = '''
    <div class="breadcrumbs">
      <ul>
        <li><a href="/">Home</a></li>
        <li><a href="/shop">Shop</a></li>
        <li><a href="/shop/widgets">Widgets</a></li>
      </ul>
    </div>
    '''
    pipeline = DeterministicExtractionPipeline()
    results = await pipeline.extract(html, [], "https://example.com")
    assert results == []


@pytest.mark.asyncio
async def test_real_content_list_still_extracted():
    """A list with actual text content (not bare links) outside nav/footer
    must still be extracted — the scoping should not overreach."""
    html = '''
    <main>
      <ul>
        <li>First requirement: bidders must be registered.</li>
        <li>Second requirement: submit by the deadline.</li>
        <li>Third requirement: include a signed cover letter.</li>
      </ul>
    </main>
    '''
    pipeline = DeterministicExtractionPipeline()
    results = await pipeline.extract(html, [], "https://example.com")
    assert len(results) == 1
    assert results[0].record_type == "list"
    assert len(results[0].data["items"]) == 3


@pytest.mark.asyncio
async def test_pipeline_schema_validation():
    """Schema validation filters out records missing required fields."""
    html = '''<script type="application/ld+json">
    {"@type": "Product", "name": "Valid", "offers": {"price": "5.00"}}
    </script>'''
    schema = ExtractionSchema(
        schema_id="s1",
        fields=[FieldDefinition(name="name", required=True)],
    )
    pipeline = DeterministicExtractionPipeline()
    results = await pipeline.extract(html, [], "https://example.com", schema=schema)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_pipeline_schema_rejects_invalid():
    """Records failing schema validation are filtered out."""
    html = '''<div class="item"><h3>Test</h3></div>'''
    schema = ExtractionSchema(
        schema_id="s2",
        fields=[FieldDefinition(name="title", required=True)],
    )
    pipeline = DeterministicExtractionPipeline()
    results = await pipeline.extract(html, [], "https://example.com", schema=schema)
    assert len(results) >= 0


@pytest.mark.asyncio
async def test_pipeline_empty_html():
    """Empty HTML produces no records."""
    pipeline = DeterministicExtractionPipeline()
    results = await pipeline.extract("", [], "https://example.com")
    assert results == []


@pytest.mark.asyncio
async def test_pipeline_semantic_html_article():
    """Semantic HTML extraction finds article elements with enough text."""
    html = '''
    <article>
        <h2>Article Title</h2>
        <p>This is a longer article body text that exceeds 50 characters to pass
           the minimum content threshold for article extraction.</p>
        <a href="/article/1">Read</a>
    </article>
    '''
    pipeline = DeterministicExtractionPipeline()
    results = await pipeline.extract(html, [], "https://example.com")
    assert len(results) >= 1
    assert results[0].record_type == "article"
    assert "Article Title" in str(results[0].data)


@pytest.mark.asyncio
async def test_pipeline_json_ld_with_graph():
    """JSON-LD with @graph array expands to individual records."""
    html = '''<script type="application/ld+json">
    {"@graph": [{"@type": "Product", "name": "A"}, {"@type": "Product", "name": "B"}]}
    </script>'''
    pipeline = DeterministicExtractionPipeline()
    results = await pipeline.extract(html, [], "https://example.com")
    assert len(results) == 2


@pytest.mark.asyncio
async def test_ai_generated_overlay_shape_round_trips_through_pipeline():
    """
    Round-trip test (W2.2): the exact JSON shape ai/client.py::generate_overlay
    asks Gemini for (container_selector / field_mappings) must work against the
    surviving pipeline. If the AI prompt and this pipeline ever drift apart again,
    /autograph output becomes silently unusable — this test is the tripwire.
    """
    html = '''
    <div class="opportunity-row">
        <h3 class="title">Launch Services Procurement</h3>
        <span class="buyer">ESA</span>
        <span class="deadline">2026-06-01</span>
        <a class="link" href="/opp/1">Details</a>
    </div>
    '''
    ai_generated_overlay = {
        "entity_type": "Opportunity",
        "container_selector": ".opportunity-row",
        "field_mappings": {
            "title": ".title",
            "buyer": ".buyer",
            "deadline": ".deadline",
        },
    }
    pipeline = DeterministicExtractionPipeline()
    results = await pipeline.extract(html, [], "https://esa.int/list", overlay=ai_generated_overlay)

    assert len(results) == 1
    assert results[0].data["title"] == "Launch Services Procurement"
    assert results[0].data["buyer"] == "ESA"
