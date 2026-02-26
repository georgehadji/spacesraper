"""
Selector Repair Agent — Mode B LLM-Powered Repair.
Single LLM call. Bounded token budget. Governance-gated activation.

# === LLM BOUNDARY: All LLM calls delegated to LLMClient ===
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from shared.contracts.global_contract import BaseAgent, AgentResult, AgentStatus
from shared.state.shared_state import get_shared_state
from storage.filesystem_storage import get_storage
from agents.selector_repair.llm_client import (
    LLMClient,
    LLMBudgetExceeded,
    LLMMalformedOutput,
)


# ---------------------------------------------------------------------------
# Canonical selector repair prompt (v1.0)
# ---------------------------------------------------------------------------

SELECTOR_REPAIR_PROMPT_V1 = """
You are a CSS selector repair specialist. Given:
- A DOM structure (JSON)
- Broken selectors and their validation errors
- Target fields and their expected content

Return ONLY a JSON object with this exact schema:
{
  "proposed_selectors": {"field_name": "css_selector", ...},
  "confidence": 0.0-1.0,
  "evidence": "brief explanation"
}

Rules:
- Output valid JSON only
- No explanation outside JSON
- Selectors must target text content matching expected values
- Do not change schema, field names, or data types
""".strip()

PROMPT_VERSION = "selector_repair_v1.0"
PROMPT_HASH    = hashlib.sha256(SELECTOR_REPAIR_PROMPT_V1.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class RepairProposal:
    proposed_selectors: Dict[str, str]
    confidence: float
    evidence: str
    tokens_used: int
    prompt_version: str
    prompt_hash: str


@dataclass
class RepairResult:
    job_id: str
    success: bool
    proposal: Optional[RepairProposal]
    proposal_uri: Optional[str]
    llm_invoked: bool
    tokens_used: int
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class SelectorRepairAgent(BaseAgent):
    """
    Mode B only. Proposes new selectors via single LLM call.
    Does NOT activate selectors — governance agent decides.

    Constraints (Global Contract §4, §5, §6):
    - Single LLM call, no retries
    - Budget from planner_decision only
    - Output persisted before governance submission
    - No schema/code changes
    - No silent activation
    """

    AGENT_NAME = "selector_repair_agent"
    MAX_TOKENS = 512  # hard cap per call

    def __init__(self, llm_client: Optional[LLMClient] = None) -> None:
        self._llm     = llm_client or LLMClient()
        self._state   = get_shared_state()
        self._storage = get_storage()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def propose_repair(
        self,
        job_id: str,
        dom_snapshot_uri: str,
        broken_selectors: Dict[str, str],
        field_errors: Dict[str, str],
        token_budget: int,
        planner_decision_ref: str,
    ) -> RepairResult:
        """
        Propose new selectors via single LLM call.

        Args:
            job_id: Owning job identifier.
            dom_snapshot_uri: URI of DOM snapshot.
            broken_selectors: field_name → failing selector.
            field_errors: field_name → error description.
            token_budget: Max tokens from planner (call skipped if <= 0).
            planner_decision_ref: URI of planner decision (audit trail).

        Returns:
            RepairResult. proposal_uri populated on success.

        Raises:
            ValueError: Invalid inputs.
            RuntimeError: Unrecoverable error (no retries).
        """
        if not job_id:
            raise ValueError("job_id required")
        if token_budget <= 0:
            return RepairResult(
                job_id=job_id,
                success=False,
                proposal=None,
                proposal_uri=None,
                llm_invoked=False,
                tokens_used=0,
                error="Token budget exhausted — LLM call skipped",
            )

        # Load DOM for context (reference only, not full content)
        try:
            dom = self._storage.read_json(dom_snapshot_uri)
        except Exception as exc:
            raise RuntimeError(f"Cannot read DOM snapshot: {exc}") from exc

        # === LLM BOUNDARY: single call ===
        try:
            llm_response = self._llm.call(
                prompt=SELECTOR_REPAIR_PROMPT_V1,
                inputs={
                    "dom_structure": dom,
                    "broken_selectors": broken_selectors,
                    "field_errors": field_errors,
                },
                max_tokens=self.MAX_TOKENS,
                token_budget=token_budget,
            )
        except LLMBudgetExceeded as exc:
            return RepairResult(
                job_id=job_id,
                success=False,
                proposal=None,
                proposal_uri=None,
                llm_invoked=False,
                tokens_used=0,
                error=str(exc),
            )
        except (LLMMalformedOutput, RuntimeError) as exc:
            # Any LLM error → explicit failure, no retry
            self._state.update_job(job_id, self.AGENT_NAME, error=str(exc))
            raise RuntimeError(f"Selector repair LLM failure: {exc}") from exc
        # === END LLM BOUNDARY ===

        # Parse structured output (already validated as JSON by LLMClient)
        parsed = json.loads(llm_response.content)
        proposal = RepairProposal(
            proposed_selectors=parsed.get("proposed_selectors", {}),
            confidence=float(parsed.get("confidence", 0.0)),
            evidence=str(parsed.get("evidence", "")),
            tokens_used=llm_response.tokens_used,
            prompt_version=PROMPT_VERSION,
            prompt_hash=PROMPT_HASH,
        )

        # Persist proposal + audit data
        audit_data = {
            "job_id": job_id,
            "planner_decision_ref": planner_decision_ref,
            "prompt_version": PROMPT_VERSION,
            "prompt_hash": PROMPT_HASH,
            "llm_input_refs": [dom_snapshot_uri],     # refs only, not content
            "llm_output": parsed,
            "tokens_used": llm_response.tokens_used,
            "timestamp": time.time(),
        }
        proposal_uri = self._storage.write_json(audit_data, f"repair_proposal_{job_id}.json")

        # Update shared state
        self._state.update_job(
            job_id, self.AGENT_NAME,
            repair_proposal_uri=proposal_uri,
            llm_invoked=True,
            tokens_used=llm_response.tokens_used,
        )
        self._state.emit_metric("repair.llm_tokens_used", llm_response.tokens_used, {"job_id": job_id})
        self._state.emit_metric("repair.llm_invocation", 1, {"job_id": job_id})

        return RepairResult(
            job_id=job_id,
            success=True,
            proposal=proposal,
            proposal_uri=proposal_uri,
            llm_invoked=True,
            tokens_used=llm_response.tokens_used,
        )

    def execute(self, **kwargs: Any) -> AgentResult:
        """BaseAgent protocol wrapper."""
        try:
            result = self.propose_repair(
                job_id=kwargs["job_id"],
                dom_snapshot_uri=kwargs["dom_snapshot_uri"],
                broken_selectors=kwargs["broken_selectors"],
                field_errors=kwargs.get("field_errors", {}),
                token_budget=kwargs.get("token_budget", 0),
                planner_decision_ref=kwargs.get("planner_decision_ref", ""),
            )
            return AgentResult(
                status=AgentStatus.SUCCESS if result.success else AgentStatus.FAILURE,
                data={
                    "proposal_uri":  result.proposal_uri,
                    "llm_invoked":   result.llm_invoked,
                    "tokens_used":   result.tokens_used,
                },
                error=result.error,
            )
        except (ValueError, RuntimeError) as exc:
            return AgentResult(status=AgentStatus.FAILURE, data={}, error=str(exc))
