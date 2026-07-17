# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Integration Tests)
# Role: Verifies the stateful handover between Extraction Kernel and Persistence Hub.

import pytest
import os
from src.application.post_processor import IntelligencePostProcessor
from src.infrastructure.storage.sqlite_tracker import SqliteTracker
from src.domain.models import Opportunity, ProcessingResult

@pytest.mark.asyncio
async def test_processor_audit_lifecycle(sample_opportunity):
    """
    Integration: Verifies that a opportunity is correctly identified as NEW 
    on first sight and UNCHANGED on the second run.
    """
    # Setup isolated test DB
    test_db = "test_audit.db"
    tracker = SqliteTracker(db_path=test_db)
    await tracker.initialize()
    
    processor = IntelligencePostProcessor()
    # Dependency Injection: Overriding the global tracker with our test instance
    # In a real app, we'd use a better DI pattern, but for this audit:
    import src.application.post_processor as pp
    pp.intel_tracker = tracker
    
    try:
        # Run 1: Discovery
        result = await processor.run_state_audit([sample_opportunity])
        assert result["NEW"] == 1
        assert sample_opportunity.change_type == "NEW"
        
        # Run 2: Re-ingestion (Unchanged)
        result2 = await processor.run_state_audit([sample_opportunity])
        assert result2["UNCHANGED"] == 1
        assert sample_opportunity.change_type == "UNCHANGED"
        
    finally:
        # Cleanup
        if os.path.exists(test_db):
            # Close connection first if needed, but aiosqlite handles it in context
            os.remove(test_db)
            if os.path.exists(f"{test_db}-wal"): os.remove(f"{test_db}-wal")
            if os.path.exists(f"{test_db}-shm"): os.remove(f"{test_db}-shm")
