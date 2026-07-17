# Shared pytest fixtures for unit and integration testing.

import pytest
from src.domain.models import Opportunity


@pytest.fixture
def sample_opportunity():
    """Provides a standardized opportunity object for transformation tests."""
    return Opportunity(
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
            <div class="opportunity-card">
                <h3>Heavy Equipment Maintenance</h3>
                <span class="ref">REF-99</span>
                <a href="/details/99">View</a>
            </div>
        </body>
    </html>
    """
