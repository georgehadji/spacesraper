# Author: Spacescraper
# Project: Spacescraper (Extraction QA Suite)
# Role: Verifies the integrity of the Generic Extraction engine.

import pytest
from src.extractors.universal_strategy import UniversalExtractionStrategy
from src.domain.models import ExtractedRecord


@pytest.mark.asyncio
async def test_extract_json_ld_product():
    """
    Scenario: Validate JSON-LD parsing produces ExtractedRecord.
    """
    html = """
    <script type="application/ld+json">
    {
        "@context": "https://schema.org/",
        "@graph": [
            {
                "@type": "Product",
                "name": "Sony WH-1000XM4",
                "image": "https://sony.com/headphone.jpg",
                "sku": "WH1000XM4B",
                "offers": {
                    "@type": "Offer",
                    "price": "348.00",
                    "priceCurrency": "EUR"
                }
            }
        ]
    }
    </script>
    """
    strategy = UniversalExtractionStrategy()
    results = await strategy.extract(html, [], "https://electronics.com")

    records = [e for e in results if isinstance(e, ExtractedRecord)]
    assert len(records) >= 1
    r = records[0]
    assert r.record_type == "product"
    assert r.data.get("name") == "Sony WH-1000XM4"
    assert r.data.get("offers", {}).get("price") == "348.00"
    assert r.source_url == "https://electronics.com"


@pytest.mark.asyncio
async def test_extract_semantic_html_listing():
    """
    Scenario: Validate semantic HTML extraction for listing-like structures.
    """
    html = """
    <ul>
        <li class="item">
            <h3 class="name">Smart Watch Ultra</h3>
            <div class="price">€299.00</div>
            <a href="/p/watch-ultra">Buy Now</a>
        </li>
    </ul>
    """
    strategy = UniversalExtractionStrategy()
    results = await strategy.extract(html, [], "https://shop.local")

    records = [e for e in results if isinstance(e, ExtractedRecord)]
    assert len(records) >= 1
    r = records[0]
    assert r.record_type in ("listing", "generic")
    assert "Smart Watch Ultra" in str(r.data)
    assert "299" in str(r.data.get("price", ""))


@pytest.mark.asyncio
async def test_extract_overlay():
    """
    Scenario: Validate overlay-driven extraction.
    """
    html = """
    <div class="product_pod">
        <h3><a href="/books/1984">1984</a></h3>
        <p class="price_color">£9.99</p>
    </div>
    """
    strategy = UniversalExtractionStrategy()
    overlay = {
        "entity_type": "book",
        "container": ".product_pod",
        "mapping": {
            "name": "h3 a",
            "price": ".price_color",
            "url": "h3 a[href]"
        }
    }
    results = await strategy.extract(html, [], "https://books.toscrape.com", overlay=overlay)

    records = [e for e in results if isinstance(e, ExtractedRecord)]
    assert len(records) == 1
    r = records[0]
    assert r.record_type == "book"
    assert r.data.get("name") == "1984"
    assert r.data.get("price") == "£9.99"
