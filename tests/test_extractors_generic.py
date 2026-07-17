# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Extraction QA Suite)
# Role: Verifies the integrity of the Universal Hybrid Extraction engine.

import pytest
from src.extractors.universal_strategy import UniversalExtractionStrategy
from src.domain.models import Product, Opportunity

@pytest.mark.asyncio
async def test_spacescraper_universal_product_heuristics():
    """
    Scenario: Validate that the Universal Extractor correctly identifies 
    unstructured product data.
    """
    html = """
    <div class="product-card">
        <h3 class="name">Smart Watch Ultra</h3>
        <div class="regular-price">€299.00</div>
        <img src="watch_ultra.png">
        <a href="/p/watch-ultra">Buy Now</a>
    </div>
    """
    strategy = UniversalExtractionStrategy()
    results = await strategy.extract(html, [], "https://shop.local")
    
    products = [e for e in results if isinstance(e, Product)]
    assert len(products) == 1
    p = products[0]
    assert p.name == "Smart Watch Ultra"
    assert p.price == 299.0
    assert "shop.local/p/watch-ultra" in p.url

@pytest.mark.asyncio
async def test_spacescraper_universal_product_json_ld():
    """
    Scenario: Validate JSON-LD parsing in Universal Strategy.
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
    
    products = [e for e in results if isinstance(e, Product)]
    assert len(products) == 1
    p = products[0]
    assert p.name == "Sony WH-1000XM4"
    assert p.price == 348.0
    assert p.id == "WH1000XM4B"

@pytest.mark.asyncio
async def test_spacescraper_universal_opportunity_heuristics():
    """
    Scenario: Validate Opportunity identification in Universal Strategy.
    """
    html = """
    <table>
        <tr class="opportunity-row">
            <td class="id">REFERENCE-2024-ABC</td>
            <td class="title">Supply of IT Equipment for City Hall</td>
            <td class="buyer">Stockholm Municipality</td>
            <td class="deadline">2024-12-15</td>
            <td class="budget">€450,000</td>
            <td><a href="/opportunities/2024-abc">View Details</a></td>
        </tr>
    </table>
    """
    strategy = UniversalExtractionStrategy()
    results = await strategy.extract(html, [], "https://opportunities.se")
    
    opportunities = [e for e in results if isinstance(e, Opportunity)]
    assert len(opportunities) == 1
    t = opportunities[0]
    assert t.external_id == "REFERENCE-2024-ABC"
    assert t.title == "Supply of IT Equipment for City Hall"
    assert t.estimated_budget == "€450,000"
    assert "opportunities.se/opportunities/2024-abc" in t.url
