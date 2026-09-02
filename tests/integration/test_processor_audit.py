# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Integration Tests)
# Role: Verifies the stateful handover between Extraction Kernel and Persistence Hub.

import pytest
from src.application.post_processor import IntelligencePostProcessor
from src.infrastructure.storage.sqlite_tracker import SqliteTracker

@pytest.mark.asyncio
async def test_processor_audit_lifecycle(sample_record, tmp_path):
    """
    Integration: Verifies that a record is correctly identified as NEW
    on first sight and UNCHANGED on the second run.
    """
    # Setup isolated test DB. Start from a clean slate so a leftover file from a
    # previous run cannot turn the first audit into an UNCHANGED result.
    test_db = str(tmp_path / "test_audit.db")
    tracker = SqliteTracker(db_path=test_db)
    await tracker.initialize()

    processor = IntelligencePostProcessor(intel_tracker=tracker)

    try:
        # Run 1: Discovery
        status_counts, audited = await processor.run_state_audit([sample_record])
        assert status_counts["NEW"] == 1
        assert len(audited) == 1
        assert sample_record.change_type == "NEW"

        # Run 2: Re-ingestion (Unchanged)
        status_counts2, _ = await processor.run_state_audit([sample_record])
        assert status_counts2["UNCHANGED"] == 1
        assert sample_record.change_type == "UNCHANGED"

    finally:
        await tracker.close()
