import pytest
from unittest.mock import AsyncMock, MagicMock
from src.domain.models import ExtractedRecord


# --- ExtractedRecord.compute_identity_hash() tests (W2.2 successor to
# pipeline.py::DataPipeline._compute_identity_hash, now ported onto the
# generic entity model instead of the deprecated Opportunity) ---

def test_extracted_record_compute_identity_hash_is_deterministic():
    """Same data must always produce the same identity hash."""
    r1 = ExtractedRecord(record_id="r1", source_url="https://esa.int/t1", data={"title": "Launch Services"})
    r2 = ExtractedRecord(record_id="r2", source_url="https://esa.int/t1", data={"title": "Launch Services"})
    r1.compute_identity_hash()
    r2.compute_identity_hash()
    assert r1.identity_hash == r2.identity_hash


def test_extracted_record_identity_hash_changes_when_data_changes():
    """A genuine data change must produce a different identity hash."""
    r1 = ExtractedRecord(record_id="r1", source_url="https://esa.int/t1", data={"title": "Launch Services"})
    r2 = ExtractedRecord(record_id="r2", source_url="https://esa.int/t1", data={"title": "Launch Services AMENDED"})
    r1.compute_identity_hash()
    r2.compute_identity_hash()
    assert r1.identity_hash != r2.identity_hash


# --- Post-processor change detection tests (W2.3: retyped from Opportunity
# to ExtractedRecord, and SqliteTracker's methods to get_record_by_id/upsert_record) ---

@pytest.mark.asyncio
async def test_unchanged_when_identity_hash_matches():
    """
    When identity_hash matches stored record, entity must be UNCHANGED —
    even if content_hash differs (simulating AI model drift).
    """
    from src.application.post_processor import IntelligencePostProcessor

    entity = ExtractedRecord(
        record_id="r1", source_url="https://esa.int/list", canonical_url="https://esa.int/t1",
        data={"title": "Launch Services"},
        content_hash="new_ai_hash_after_model_update",
        identity_hash="stable_raw_hash",
    )

    stored_record = {
        "content_hash": "old_ai_hash_before_model_update",
        "identity_hash": "stable_raw_hash",  # same → UNCHANGED
        "first_seen": "2026-01-01T00:00:00"
    }

    tracker = MagicMock()
    tracker.get_record_by_id = AsyncMock(return_value=stored_record)
    tracker.upsert_record = AsyncMock()
    processor = IntelligencePostProcessor(intel_tracker=tracker)

    counts, audited = await processor.run_state_audit([entity])

    assert counts["UNCHANGED"] == 1
    assert counts["UPDATED"] == 0


@pytest.mark.asyncio
async def test_updated_when_identity_hash_changes():
    """When identity_hash differs from stored, entity must be UPDATED."""
    from src.application.post_processor import IntelligencePostProcessor

    entity = ExtractedRecord(
        record_id="r1", source_url="https://esa.int/list", canonical_url="https://esa.int/t1",
        data={"title": "Launch Services v2"},
        content_hash="some_hash",
        identity_hash="new_raw_hash",  # changed
    )

    stored_record = {
        "content_hash": "some_hash",
        "identity_hash": "old_raw_hash",  # different → UPDATED
        "first_seen": "2026-01-01T00:00:00"
    }

    tracker = MagicMock()
    tracker.get_record_by_id = AsyncMock(return_value=stored_record)
    tracker.upsert_record = AsyncMock()
    processor = IntelligencePostProcessor(intel_tracker=tracker)

    counts, audited = await processor.run_state_audit([entity])

    assert counts["UPDATED"] == 1
    assert counts["UNCHANGED"] == 0


@pytest.mark.asyncio
async def test_unchanged_not_silently_suppressed_when_entity_has_no_identity_hash():
    """
    When stored record HAS identity_hash but incoming entity does NOT,
    fall back to content_hash comparison rather than silently marking UNCHANGED.
    """
    from src.application.post_processor import IntelligencePostProcessor

    entity = ExtractedRecord(
        record_id="r1", source_url="https://esa.int/list", canonical_url="https://esa.int/t1",
        data={"title": "Launch Services"},
        content_hash="changed_content_hash",
        identity_hash=None,  # Not set (entity not routed through pipeline)
    )

    stored_record = {
        "content_hash": "original_content_hash",  # different → should be UPDATED
        "identity_hash": "some_stored_hash",       # stored has it, entity does not
        "first_seen": "2026-01-01T00:00:00"
    }

    tracker = MagicMock()
    tracker.get_record_by_id = AsyncMock(return_value=stored_record)
    tracker.upsert_record = AsyncMock()
    processor = IntelligencePostProcessor(intel_tracker=tracker)

    counts, audited = await processor.run_state_audit([entity])

    # Must detect the change via content_hash fallback, not suppress as UNCHANGED
    assert counts["UPDATED"] == 1
    assert counts["UNCHANGED"] == 0
