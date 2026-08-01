# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Post-Processing Hub)
# Role: Lean state auditing and change detection focusing on Epistemic Clarity.

import logging
from datetime import datetime, timezone
from typing import List, Any, Dict, Tuple
from src.domain.models import Opportunity
from src.infrastructure.storage.sqlite_tracker import SqliteTracker
from src.domain.exceptions import StorageError

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

    async def run_state_audit(self, entities: List[Any]) -> Tuple[Dict[str, int], List[Opportunity]]:
        """
        Performs stateful change detection and persistence via SQLite.
        Returns a summary of changes and the list of audited opportunities.
        """
        status_counts = {"NEW": 0, "UPDATED": 0, "UNCHANGED": 0}
        audited_opportunities = []
        
        for entity in entities:
            if not isinstance(entity, Opportunity):
                continue

            # Resolve canonical state
            opportunity_id = entity.url 
            prev_state = await self.intel_tracker.get_opportunity_by_id(opportunity_id)
            
            if not prev_state:
                entity.change_type = "NEW"
                entity.first_seen = datetime.now(tz=timezone.utc)
                status_counts["NEW"] += 1
            elif (
                prev_state.get('identity_hash') and entity.identity_hash
                and prev_state['identity_hash'] != entity.identity_hash
            ):
                entity.change_type = "UPDATED"
                entity.first_seen = datetime.fromisoformat(prev_state['first_seen'])
                entity.last_seen = datetime.now(tz=timezone.utc)
                status_counts["UPDATED"] += 1
            elif not prev_state.get('identity_hash'):
                # Legacy record without identity_hash — fall back to content_hash comparison
                if prev_state.get('content_hash') != entity.content_hash:
                    entity.change_type = "UPDATED"
                    entity.first_seen = datetime.fromisoformat(prev_state['first_seen'])
                    entity.last_seen = datetime.now(tz=timezone.utc)
                    status_counts["UPDATED"] += 1
                else:
                    entity.change_type = "UNCHANGED"
                    entity.last_seen = datetime.now(tz=timezone.utc)
                    status_counts["UNCHANGED"] += 1
            elif prev_state.get('identity_hash') and not entity.identity_hash:
                # Entity arrived without identity_hash (not routed through pipeline).
                # Fall back to content_hash comparison for safety.
                if prev_state.get('content_hash') != entity.content_hash:
                    entity.change_type = "UPDATED"
                    entity.first_seen = datetime.fromisoformat(prev_state['first_seen'])
                    entity.last_seen = datetime.now(tz=timezone.utc)
                    status_counts["UPDATED"] += 1
                else:
                    entity.change_type = "UNCHANGED"
                    entity.last_seen = datetime.now(tz=timezone.utc)
                    status_counts["UNCHANGED"] += 1
            else:
                entity.change_type = "UNCHANGED"
                entity.last_seen = datetime.now(tz=timezone.utc)
                status_counts["UNCHANGED"] += 1
            
            try:
                await self.intel_tracker.upsert_opportunity(entity)
                audited_opportunities.append(entity)
            except Exception as e:
                logger.error(f"Audit Persistence Failure: {e}")
                # We continue auditing even if one fails
            
        return status_counts, audited_opportunities
