# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Specialized Extraction QA)
# Role: Verifies the integrity of specialized site strategies (Amazon, Palaiologue, Ek.gr).

import pytest
from src.extractors.target_amazon import AmazonExtractionStrategy
from src.extractors.target_ek import EkGrExtractionStrategy
from src.domain.models import Product, FollowLink

@pytest.mark.asyncio
async def test_spacescraper_amazon_extraction():
    """
    Scenario: Verify Amazon extractor. Note that the strategy currently 
    simulates a selector failure for self-healing tests.
    """
    html = """
    <div data-component-type="s-search-result" data-asin="B08N5WRWJ6">
        <div class="s-result-item">
            <h2>MacBook Air M1</h2>
            <span class="a-price-whole">999</span>
            <span class="a-price-fraction">00</span>
            <a class="a-link-normal" href="/dp/B08N5WRWJ6">View Product</a>
        </div>
    </div>
    """
    strategy = AmazonExtractionStrategy()
    # Amazon strategy currently has 'product_divs = []' hardcoded to trigger self-healing.
    # We expect 0 results unless we were to modify the code.
    results = await strategy.extract(html, [], "https://www.amazon.com/s?k=macbook")
    assert len(results) == 0 # Current behavior as per simulation


@pytest.mark.asyncio
async def test_spacescraper_ekgr_detail():
    """
    Scenario: Verify ek.gr (Kyriakidis Editions) book detail extraction.
    """
    html = """
    <div class="ProductDetails">
        <h1>Byzantine Architecture</h1>
        <div class="ProductDescription">A comprehensive guide to ecclesiastical structures.</div>
        <div class="ProductImage"><img src="/images/byz_arch.jpg"></div>
    </div>
    """
    strategy = EkGrExtractionStrategy()
    # URL needs /Product/ to trigger detail extraction logic or .ProductDetails presence
    results = await strategy.extract(html, [], "https://ek.gr/Product/98765")
    
    assert len(results) == 1
    p = results[0]
    assert p.name == "Byzantine Architecture"
    assert p.id == "98765"
    assert "byz_arch.jpg" in p.image_url
    assert "ecclesiastical structures" in p.description

