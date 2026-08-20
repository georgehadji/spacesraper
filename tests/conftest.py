# Shared pytest fixtures for unit and integration testing.

import pytest
from src.domain.models import ExtractedRecord


@pytest.fixture
def sample_record():
    """Provides a standardized ExtractedRecord for transformation tests."""
    return ExtractedRecord(
        record_id="rec_test_100",
        record_type="opportunity",
        source_url="https://example.com/list",
        canonical_url="https://example.com/t100",
        data={"title": "Infrastructure Development Project", "buyer": "Ministry of Defense"},
        content_hash="abc_123",
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
