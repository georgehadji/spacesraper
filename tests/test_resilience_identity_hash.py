import pytest
import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch
from src.domain.models import Opportunity, RawScrapePayload
from src.application.pipeline import DataPipeline


# --- Model tests ---

def test_opportunity_has_identity_hash_field():
    """Opportunity must expose an identity_hash field."""
    t = Opportunity(
        source="test", title="Launch Services", url="https://esa.int/t1",
        source_url="https://esa.int/list"
    )
    assert hasattr(t, "identity_hash")


def test_identity_hash_is_none_by_default():
    """identity_hash starts as None before pipeline sets it."""
    t = Opportunity(
        source="test", title="Launch Services", url="https://esa.int/t1",
        source_url="https://esa.int/list"
    )
    assert t.identity_hash is None


# --- Pipeline tests ---

def test_compute_identity_hash_uses_raw_fields():
    """Identity hash must derive from url + raw title + raw deadline only."""
    pipeline = DataPipeline(ai_enrichment_enabled=False)
    t = Opportunity(
        source="esa", title="Launch Services", url="https://esa.int/t1",
        source_url="https://esa.int/list", deadline="2026-06-01"
    )
    pipeline._compute_identity_hash(t)
    expected = hashlib.md5("https://esa.int/t1|Launch Services|2026-06-01".encode()).hexdigest()
    assert t.identity_hash == expected


def test_identity_hash_stable_when_ai_changes_title():
    """
    Simulates an AI model update that rewrites the title.
    Identity hash must be identical before and after AI mutation.
    """
    pipeline = DataPipeline(ai_enrichment_enabled=False)
    t = Opportunity(
        source="esa", title="Launch Services", url="https://esa.int/t1",
        source_url="https://esa.int/list", deadline="2026-06-01"
    )
    # Compute identity hash on raw title
    pipeline._compute_identity_hash(t)
    hash_before = t.identity_hash

    # Simulate AI model rewriting the title
    t.title = "Space Launch Services Procurement - EU"
    t.summary = "Updated summary from new model"

    # The pre-AI snapshot stays stable.
    assert hash_before == hashlib.md5("https://esa.int/t1|Launch Services|2026-06-01".encode()).hexdigest()


def test_identity_hash_changes_on_real_update():
    """If raw title genuinely changes (real update), identity hash must change."""
    pipeline = DataPipeline(ai_enrichment_enabled=False)
    t1 = Opportunity(
        source="esa", title="Launch Services", url="https://esa.int/t1",
        source_url="https://esa.int/list", deadline="2026-06-01"
    )
    t2 = Opportunity(
        source="esa", title="Launch Services AMENDED", url="https://esa.int/t1",
        source_url="https://esa.int/list", deadline="2026-06-01"
    )
    pipeline._compute_identity_hash(t1)
    pipeline._compute_identity_hash(t2)
    assert t1.identity_hash != t2.identity_hash


# --- Post-processor change detection tests ---

@pytest.mark.asyncio
async def test_unchanged_when_identity_hash_matches():
    """
    When identity_hash matches stored record, entity must be UNCHANGED —
    even if content_hash differs (simulating AI model drift).
    """
    from src.application.post_processor import IntelligencePostProcessor

    entity = Opportunity(
        source="esa", title="Launch Services", url="https://esa.int/t1",
        source_url="https://esa.int/list",
        content_hash="new_ai_hash_after_model_update",
        identity_hash="stable_raw_hash"
    )

    stored_record = {
        "content_hash": "old_ai_hash_before_model_update",
        "identity_hash": "stable_raw_hash",  # same → UNCHANGED
        "first_seen": "2026-01-01T00:00:00"
    }

    processor = IntelligencePostProcessor()
    processor.intel_tracker = MagicMock()
    processor.intel_tracker.get_opportunity_by_id = AsyncMock(return_value=stored_record)
    processor.intel_tracker.upsert_opportunity = AsyncMock()

    counts, audited = await processor.run_state_audit([entity])

    assert counts["UNCHANGED"] == 1
    assert counts["UPDATED"] == 0


@pytest.mark.asyncio
async def test_updated_when_identity_hash_changes():
    """When identity_hash differs from stored, entity must be UPDATED."""
    from src.application.post_processor import IntelligencePostProcessor

    entity = Opportunity(
        source="esa", title="Launch Services v2", url="https://esa.int/t1",
        source_url="https://esa.int/list",
        content_hash="some_hash",
        identity_hash="new_raw_hash"  # changed
    )

    stored_record = {
        "content_hash": "some_hash",
        "identity_hash": "old_raw_hash",  # different → UPDATED
        "first_seen": "2026-01-01T00:00:00"
    }

    processor = IntelligencePostProcessor()
    processor.intel_tracker = MagicMock()
    processor.intel_tracker.get_opportunity_by_id = AsyncMock(return_value=stored_record)
    processor.intel_tracker.upsert_opportunity = AsyncMock()

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

    entity = Opportunity(
        source="esa", title="Launch Services", url="https://esa.int/t1",
        source_url="https://esa.int/list",
        content_hash="changed_content_hash",
        identity_hash=None  # Not set (entity not routed through pipeline)
    )

    stored_record = {
        "content_hash": "original_content_hash",  # different → should be UPDATED
        "identity_hash": "some_stored_hash",       # stored has it, entity does not
        "first_seen": "2026-01-01T00:00:00"
    }

    processor = IntelligencePostProcessor()
    processor.intel_tracker = MagicMock()
    processor.intel_tracker.get_opportunity_by_id = AsyncMock(return_value=stored_record)
    processor.intel_tracker.upsert_opportunity = AsyncMock()

    counts, audited = await processor.run_state_audit([entity])

    # Must detect the change via content_hash fallback, not suppress as UNCHANGED
    assert counts["UPDATED"] == 1
    assert counts["UNCHANGED"] == 0
