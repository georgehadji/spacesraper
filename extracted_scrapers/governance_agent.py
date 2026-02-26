"""
Governance Agent — Selector Proposal Review and Versioning.
Validates proposed selectors, records approval/rejection, versions artifacts.
Never activates selectors automatically.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from shared.contracts.global_contract import BaseAgent, AgentResult, AgentStatus
from shared.state.shared_state import get_shared_state, GovernanceDecision
from storage.filesystem_storage import get_storage


@dataclass
class GovernanceRecord:
    job_id: str
    decision: GovernanceDecision
    proposed_selectors: Dict[str, str]
    version: str
    reviewed_at: float
    rejection_reason: Optional[str] = None
    governance_uri: Optional[str] = None


class GovernanceAgent(BaseAgent):
    """
    Reviews repair proposals and records governance decisions.

    Constraints (Global Contract §6, §8):
    - No selector change without validation
    - Every repair traceable
    - Structural changes require version increment
    - Rollback always possible
    """

    AGENT_NAME = "governance_agent"

    def __init__(self) -> None:
        self._state   = get_shared_state()
        self._storage = get_storage()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def review(
        self,
        job_id: str,
        proposal_uri: str,
        original_selectors: Dict[str, str],
    ) -> GovernanceRecord:
        """
        Review a repair proposal and record decision.

        Approval criteria:
        - All proposed selectors non-empty strings
        - At least one selector differs from original (actual repair)
        - Confidence >= 0.5

        Args:
            job_id: Owning job.
            proposal_uri: URI of persisted repair proposal.
            original_selectors: Current active selectors for comparison.

        Returns:
            GovernanceRecord with decision and governance_uri.
        """
        if not job_id:
            raise ValueError("job_id required")
        if not proposal_uri:
            raise ValueError("proposal_uri required")

        try:
            proposal_data = self._storage.read_json(proposal_uri)
        except Exception as exc:
            raise RuntimeError(f"Cannot read proposal: {exc}") from exc

        llm_output  = proposal_data.get("llm_output", {})
        proposed    = llm_output.get("proposed_selectors", {})
        confidence  = float(llm_output.get("confidence", 0.0))

        decision, reason = self._evaluate(proposed, original_selectors, confidence)
        version     = self._next_version(job_id)

        record_data = {
            "job_id":               job_id,
            "decision":             decision.value,
            "proposed_selectors":   proposed,
            "original_selectors":   original_selectors,
            "confidence":           confidence,
            "version":              version,
            "reviewed_at":          time.time(),
            "rejection_reason":     reason,
            "proposal_uri":         proposal_uri,
        }
        governance_uri = self._storage.write_json(record_data, f"governance_{job_id}.json")

        self._state.update_job(
            job_id, self.AGENT_NAME,
            governance_decision=decision,
            repair_approved=(decision == GovernanceDecision.APPROVED),
        )
        self._state.emit_metric(
            "governance.decision",
            decision.value,
            {"job_id": job_id},
        )

        return GovernanceRecord(
            job_id=job_id,
            decision=decision,
            proposed_selectors=proposed,
            version=version,
            reviewed_at=record_data["reviewed_at"],
            rejection_reason=reason,
            governance_uri=governance_uri,
        )

    def execute(self, **kwargs: Any) -> AgentResult:
        try:
            record = self.review(
                job_id=kwargs["job_id"],
                proposal_uri=kwargs["proposal_uri"],
                original_selectors=kwargs.get("original_selectors", {}),
            )
            return AgentResult(
                status=AgentStatus.SUCCESS,
                data={
                    "decision":         record.decision.value,
                    "governance_uri":   record.governance_uri,
                    "repair_approved":  record.decision == GovernanceDecision.APPROVED,
                },
            )
        except (ValueError, RuntimeError) as exc:
            return AgentResult(status=AgentStatus.FAILURE, data={}, error=str(exc))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _evaluate(
        self,
        proposed: Dict[str, str],
        original: Dict[str, str],
        confidence: float,
    ) -> tuple[GovernanceDecision, Optional[str]]:
        if not proposed:
            return GovernanceDecision.REJECTED, "No selectors proposed"

        # All values must be non-empty strings
        for field, sel in proposed.items():
            if not isinstance(sel, str) or not sel.strip():
                return GovernanceDecision.REJECTED, f"Invalid selector for field '{field}'"

        # At least one selector must differ
        changed = any(proposed.get(k) != v for k, v in original.items())
        if not changed:
            return GovernanceDecision.REJECTED, "Proposed selectors identical to current"

        if confidence < 0.5:
            return GovernanceDecision.REJECTED, f"Confidence too low: {confidence:.2f}"

        return GovernanceDecision.APPROVED, None

    def _next_version(self, job_id: str) -> str:
        ts = int(time.time())
        return f"v_{job_id}_{ts}"
