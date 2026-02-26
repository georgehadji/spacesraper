# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Post-Processing Hub)
# Role: Lean state auditing and change detection focusing on Epistemic Clarity.

import logging
from datetime import datetime
from typing import List, Any, Dict, Tuple
from src.domain.models import Tender
from src.infrastructure.storage.sqlite_tracker import intel_tracker
from src.application.classifier import tender_classifier
from src.domain.exceptions import StorageError

logger = logging.getLogger("Spacescraper.PostProcessor")

class IntelligencePostProcessor:
    """
    Spacescraper Audit Hub.
    Handles persistence and state resolution. Side-effects are deferred to the event loop.
    """

    def __init__(self):
        self.intel_tracker = intel_tracker

    async def run_state_audit(self, entities: List[Any]) -> Tuple[Dict[str, int], List[Tender]]:
        """
        Performs stateful change detection and persistence via SQLite.
        Returns a summary of changes and the list of audited tenders.
        """
        status_counts = {"NEW": 0, "UPDATED": 0, "UNCHANGED": 0}
        audited_tenders = []
        
        for entity in entities:
            if not isinstance(entity, Tender):
                continue

            # Apply classification heuristics
            entity.classification = tender_classifier.classify(entity.title)

            # Resolve canonical state
            tender_id = entity.url 
            prev_state = await self.intel_tracker.get_tender_by_id(tender_id)
            
            if not prev_state:
                entity.change_type = "NEW"
                entity.first_seen = datetime.utcnow()
                status_counts["NEW"] += 1
            elif (
                prev_state.get('identity_hash') and entity.identity_hash
                and prev_state['identity_hash'] != entity.identity_hash
            ):
                entity.change_type = "UPDATED"
                entity.first_seen = datetime.fromisoformat(prev_state['first_seen'])
                entity.last_seen = datetime.utcnow()
                status_counts["UPDATED"] += 1
            elif not prev_state.get('identity_hash'):
                # Legacy record without identity_hash — fall back to content_hash comparison
                if prev_state.get('content_hash') != entity.content_hash:
                    entity.change_type = "UPDATED"
                    entity.first_seen = datetime.fromisoformat(prev_state['first_seen'])
                    entity.last_seen = datetime.utcnow()
                    status_counts["UPDATED"] += 1
                else:
                    entity.change_type = "UNCHANGED"
                    entity.last_seen = datetime.utcnow()
                    status_counts["UNCHANGED"] += 1
            elif prev_state.get('identity_hash') and not entity.identity_hash:
                # Entity arrived without identity_hash (not routed through pipeline).
                # Fall back to content_hash comparison for safety.
                if prev_state.get('content_hash') != entity.content_hash:
                    entity.change_type = "UPDATED"
                    entity.first_seen = datetime.fromisoformat(prev_state['first_seen'])
                    entity.last_seen = datetime.utcnow()
                    status_counts["UPDATED"] += 1
                else:
                    entity.change_type = "UNCHANGED"
                    entity.last_seen = datetime.utcnow()
                    status_counts["UNCHANGED"] += 1
            else:
                entity.change_type = "UNCHANGED"
                entity.last_seen = datetime.utcnow()
                status_counts["UNCHANGED"] += 1
            
            try:
                await self.intel_tracker.upsert_tender(entity)
                audited_tenders.append(entity)
            except Exception as e:
                logger.error(f"Audit Persistence Failure: {e}")
                # We continue auditing even if one fails
            
        return status_counts, audited_tenders
