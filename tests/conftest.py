# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Test Infrastructure)
# Role: Shared pytest fixtures for unit and integration testing.

import pytest
import asyncio
import os
from src.domain.models import Tender
from datetime import datetime

@pytest.fixture
def sample_tender():
    """Provides a standardized tender object for transformation tests."""
    return Tender(
        source="Test Source",
        external_id="T-100",
        title="Infrastructure Development Project",
        buyer="Ministry of Defense",
        url="https://example.com/t100",
        source_url="https://example.com/list",
        content_hash="abc_123"
    )

@pytest.fixture
def mock_html_listing():
    """Sample HTML for extraction strategy testing."""
    return """
    <html>
        <body>
            <div class="tender-card">
                <h3>Heavy Equipment Maintenance</h3>
                <span class="ref">REF-99</span>
                <a href="/details/99">View</a>
            </div>
        </body>
    </html>
    """

@pytest.fixture(scope="session")
def event_loop():
    """Ensure a consistent event loop for async tests across the cluster."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()
