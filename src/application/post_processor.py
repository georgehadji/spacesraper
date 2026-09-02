# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Post-Processing Hub)
# Role: Lean state auditing and change detection focusing on Epistemic Clarity.

import logging
from datetime import UTC, datetime
from typing import Any

from src.domain.models import ChangeType, ExtractedRecord
from src.infrastructure.storage.sqlite_tracker import SqliteTracker

logger = logging.getLogger("Spacescraper.PostProcessor")

class IntelligencePostProcessor:
    """
    Spacescraper Audit Hub.
    Handles persistence and state resolution.
    """

    def __init__(self, intel_tracker: SqliteTracker):
        if intel_tracker is None:
            raise ValueError("IntelligencePostProcessor requires an intel_tracker instance.")
        self.intel_tracker = intel_tracker

    async def run_state_audit(self, entities: list[Any]) -> tuple[dict[str, int], list[ExtractedRecord]]:
        """
        Performs stateful change detection and persistence via SQLite.
        Returns a summary of changes and the list of audited records.
        """
        status_counts = {"NEW": 0, "UPDATED": 0, "UNCHANGED": 0}
        audited_records = []

        for entity in entities:
            if not isinstance(entity, ExtractedRecord):
                continue

            # Resolve canonical state — same identity key Deduplicator uses.
            record_key = entity.canonical_url or entity.source_url
            prev_state = await self.intel_tracker.get_record_by_id(record_key)

            if not prev_state:
                entity.change_type = ChangeType.NEW
                entity.first_seen = datetime.now(tz=UTC)
                status_counts["NEW"] += 1
            elif (
                prev_state.get('identity_hash') and entity.identity_hash
                and prev_state['identity_hash'] != entity.identity_hash
            ):
                entity.change_type = ChangeType.UPDATED
                entity.first_seen = datetime.fromisoformat(prev_state['first_seen'])
                entity.last_seen = datetime.now(tz=UTC)
                status_counts["UPDATED"] += 1
            elif not prev_state.get('identity_hash'):
                # Legacy record without identity_hash — fall back to content_hash comparison
                if prev_state.get('content_hash') != entity.content_hash:
                    entity.change_type = ChangeType.UPDATED
                    entity.first_seen = datetime.fromisoformat(prev_state['first_seen'])
                    entity.last_seen = datetime.now(tz=UTC)
                    status_counts["UPDATED"] += 1
                else:
                    entity.change_type = ChangeType.UNCHANGED
                    entity.last_seen = datetime.now(tz=UTC)
                    status_counts["UNCHANGED"] += 1
            elif prev_state.get('identity_hash') and not entity.identity_hash:
                # Entity arrived without identity_hash (not routed through pipeline).
                # Fall back to content_hash comparison for safety.
                if prev_state.get('content_hash') != entity.content_hash:
                    entity.change_type = ChangeType.UPDATED
                    entity.first_seen = datetime.fromisoformat(prev_state['first_seen'])
                    entity.last_seen = datetime.now(tz=UTC)
                    status_counts["UPDATED"] += 1
                else:
                    entity.change_type = ChangeType.UNCHANGED
                    entity.last_seen = datetime.now(tz=UTC)
                    status_counts["UNCHANGED"] += 1
            else:
                entity.change_type = ChangeType.UNCHANGED
                entity.last_seen = datetime.now(tz=UTC)
                status_counts["UNCHANGED"] += 1

            try:
                await self.intel_tracker.upsert_record(entity)
                audited_records.append(entity)
            except Exception as e:
                logger.error(f"Audit Persistence Failure: {e}")
                # We continue auditing even if one fails

        return status_counts, audited_records
