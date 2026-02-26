# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Extraction QA)
# Role: Basic validation for the Generic/Heuristic extraction engine.

import pytest
from src.extractors.target_generic import GenericExtractionStrategy

@pytest.mark.asyncio
async def test_spacescraper_heuristic_logic():
    """
    Scenario: Verify that the Generic Strategy can resolve unstructured 
    product data using common CSS class signatures.
    """
    html = """
    <div class="product-item">
        <h2 class="product-title">Spacescraper Pro 2026</h2>
        <span class="price">€99.00</span>
        <img src="https://spacescraper.ai/thumb.jpg">
        <p>Premium web intelligence module.</p>
        <a href="https://spacescraper.ai/v1">Explore Node</a>
    </div>
    """
    
    # Initialize the strategy node
    strategy = GenericExtractionStrategy()
    
    # Execute extraction with base URL resolution
    results = await strategy.extract(html, [], "https://spacescraper.ai")
    
    # Validation Logic
    assert len(results) >= 1
    p = results[0]
    assert p.name == "Spacescraper Pro 2026"
    assert p.price == 99.0
    assert p.image_url == "https://spacescraper.ai/thumb.jpg"
    assert "spacescraper.ai/v1" in p.url
