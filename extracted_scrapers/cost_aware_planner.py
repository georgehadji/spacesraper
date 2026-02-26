"""
Cost-Aware Planner — Execution Mode Selection and Token Budget Allocation.
Selects cheapest viable mode. Logs all planning decisions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from shared.contracts.global_contract import BaseAgent, AgentResult, AgentStatus
from shared.state.shared_state import get_shared_state, ExecutionMode
from storage.filesystem_storage import get_storage


@dataclass
class PlannerDecision:
    job_id: str
    selected_mode: ExecutionMode
    token_budget: int
    rationale: str
    decision_uri: str
    decided_at: float


class CostAwarePlanner(BaseAgent):
    """
    Selects execution mode and allocates token budget.
    Always prefers Mode A (deterministic, free).
    Mode B only when explicitly requested and budget available.

    Constraints (Global Contract §5):
    - Cheapest viable mode selected
    - Token usage bounded
    - Planning decisions logged
    - Recursive LLM calls forbidden
    """

    AGENT_NAME         = "cost_aware_planner"
    DEFAULT_BUDGET_B   = 1000   # tokens for Mode B
    MAX_BUDGET_B       = 2000   # hard ceiling

    def __init__(self) -> None:
        self._state   = get_shared_state()
        self._storage = get_storage()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan(
        self,
        job_id: str,
        requested_mode: ExecutionMode,
        token_allowance: Optional[int] = None,
    ) -> PlannerDecision:
        """
        Produce a planning decision for the job.

        Args:
            job_id: Owning job.
            requested_mode: Desired mode from caller.
            token_allowance: Optional override for token budget.

        Returns:
            PlannerDecision with selected mode and token budget.
        """
        if not job_id:
            raise ValueError("job_id required")

        if requested_mode == ExecutionMode.A:
            mode   = ExecutionMode.A
            budget = 0
            rationale = "Mode A selected: deterministic fast-path, no LLM required"
        else:
            # Mode B
            budget = min(
                token_allowance if token_allowance is not None else self.DEFAULT_BUDGET_B,
                self.MAX_BUDGET_B,
            )
            mode   = ExecutionMode.B
            rationale = f"Mode B selected: LLM repair with budget {budget} tokens"

        decision_data = {
            "job_id":        job_id,
            "selected_mode": mode.value,
            "token_budget":  budget,
            "rationale":     rationale,
            "decided_at":    time.time(),
        }
        uri = self._storage.write_json(decision_data, f"planner_{job_id}.json")

        self._state.update_job(
            job_id, self.AGENT_NAME,
            planner_decision_ref=uri,
            token_budget=budget,
        )
        self._state.emit_metric("planner.mode_selected", mode.value, {"job_id": job_id})
        self._state.emit_metric("planner.token_budget",  budget,     {"job_id": job_id})

        return PlannerDecision(
            job_id=job_id,
            selected_mode=mode,
            token_budget=budget,
            rationale=rationale,
            decision_uri=uri,
            decided_at=decision_data["decided_at"],
        )

    def execute(self, **kwargs: Any) -> AgentResult:
        try:
            decision = self.plan(
                job_id=kwargs["job_id"],
                requested_mode=ExecutionMode(kwargs.get("mode", "A")),
                token_allowance=kwargs.get("token_allowance"),
            )
            return AgentResult(
                status=AgentStatus.SUCCESS,
                data={
                    "selected_mode":  decision.selected_mode.value,
                    "token_budget":   decision.token_budget,
                    "decision_uri":   decision.decision_uri,
                },
            )
        except (ValueError, RuntimeError) as exc:
            return AgentResult(status=AgentStatus.FAILURE, data={}, error=str(exc))
