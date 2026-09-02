# Tests for IntelligencePostProcessor.run_state_audit.
#
# W2.3 update: retyped from Opportunity to ExtractedRecord (C2 fix). Originally
# (W0.9) this file documented C2 via an xfail — run_state_audit() only recognized
# Opportunity entities, but the live extraction strategy emitted only
# ExtractedRecord, so every entity was silently discarded on every job with no
# log, exception, or metric. Now that post_processor.py is retyped against
# ExtractedRecord (docs/plans/2026-08-10-architecture-remediation-to-8.5.md,
# W2.3), that xfail is gone — the real behavior is asserted directly below.

from datetime import datetime, timezone

import pytest

from src.application.post_processor import IntelligencePostProcessor
from src.domain.models import ExtractedRecord


class FakeIntelTracker:
    """In-memory stand-in for SqliteTracker — isolates the audit logic under test."""

    def __init__(self):
        self._rows: dict[str, dict] = {}
        self.upsert_calls: list[ExtractedRecord] = []
        self.raise_on_upsert = False

    async def get_record_by_id(self, record_key: str):
        return self._rows.get(record_key)

    async def upsert_record(self, record: ExtractedRecord) -> bool:
        if self.raise_on_upsert:
            raise RuntimeError("simulated persistence failure")
        key = record.canonical_url or record.source_url
        is_new = key not in self._rows
        self.upsert_calls.append(record)
        self._rows[key] = {
            "identity_hash": record.identity_hash,
            "content_hash": record.content_hash,
            "first_seen": record.first_seen.isoformat(),
        }
        return is_new


def _make_record(**overrides) -> ExtractedRecord:
    defaults = dict(
        record_id="rec_1",
        record_type="opportunity",
        source_url="https://example.com/list",
        canonical_url="https://example.com/opp/1",
        data={"title": "Test Opportunity"},
        identity_hash="hash-a",
        content_hash="content-a",
    )
    defaults.update(overrides)
    return ExtractedRecord(**defaults)


@pytest.mark.asyncio
async def test_init_requires_intel_tracker():
    with pytest.raises(ValueError):
        IntelligencePostProcessor(intel_tracker=None)


@pytest.mark.asyncio
async def test_new_record_is_recorded_as_new():
    tracker = FakeIntelTracker()
    processor = IntelligencePostProcessor(intel_tracker=tracker)

    counts, audited = await processor.run_state_audit([_make_record()])

    assert counts == {"NEW": 1, "UPDATED": 0, "UNCHANGED": 0}
    assert len(audited) == 1
    assert audited[0].change_type == "NEW"
    assert len(tracker.upsert_calls) == 1


@pytest.mark.asyncio
async def test_existing_record_with_changed_identity_hash_is_updated():
    tracker = FakeIntelTracker()
    tracker._rows["https://example.com/opp/1"] = {
        "identity_hash": "hash-old",
        "content_hash": "content-old",
        "first_seen": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
    }
    processor = IntelligencePostProcessor(intel_tracker=tracker)

    counts, audited = await processor.run_state_audit(
        [_make_record(identity_hash="hash-new")]
    )

    assert counts == {"NEW": 0, "UPDATED": 1, "UNCHANGED": 0}
    assert audited[0].change_type == "UPDATED"
    assert audited[0].first_seen == datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_existing_record_with_same_identity_hash_is_unchanged():
    tracker = FakeIntelTracker()
    tracker._rows["https://example.com/opp/1"] = {
        "identity_hash": "hash-a",
        "content_hash": "content-a",
        "first_seen": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
    }
    processor = IntelligencePostProcessor(intel_tracker=tracker)

    counts, audited = await processor.run_state_audit([_make_record()])

    assert counts == {"NEW": 0, "UPDATED": 0, "UNCHANGED": 1}
    assert audited[0].change_type == "UNCHANGED"


@pytest.mark.asyncio
async def test_persistence_failure_is_logged_and_does_not_raise():
    """A failed upsert must not abort the batch or the entity's counted status."""
    tracker = FakeIntelTracker()
    tracker.raise_on_upsert = True
    processor = IntelligencePostProcessor(intel_tracker=tracker)

    counts, audited = await processor.run_state_audit([_make_record()])

    assert counts == {"NEW": 1, "UPDATED": 0, "UNCHANGED": 0}
    assert audited == []  # entity is counted, but not returned since upsert failed


@pytest.mark.asyncio
async def test_non_extracted_record_entities_are_skipped():
    """Only ExtractedRecord instances participate in the audit; other payloads pass through untouched."""
    tracker = FakeIntelTracker()
    processor = IntelligencePostProcessor(intel_tracker=tracker)

    counts, audited = await processor.run_state_audit([{"not": "a record"}, "plain string", 42])

    assert counts == {"NEW": 0, "UPDATED": 0, "UNCHANGED": 0}
    assert audited == []


@pytest.mark.asyncio
async def test_extracted_record_entities_are_audited_not_silently_dropped():
    """
    C2 regression test. Before W2.3, run_state_audit() filtered
    isinstance(entity, Opportunity), but the live extraction strategy emitted
    only ExtractedRecord — every real entity was silently dropped on every job.
    This must now pass unconditionally (no xfail): the bug is fixed.
    """
    tracker = FakeIntelTracker()
    processor = IntelligencePostProcessor(intel_tracker=tracker)
    record = ExtractedRecord(
        record_id="rec-1",
        source_url="https://example.com/rec/1",
        canonical_url="https://example.com/rec/1",
        data={"title": "Test Record"},
    )

    counts, audited = await processor.run_state_audit([record])

    assert counts["NEW"] == 1
    assert len(audited) == 1
