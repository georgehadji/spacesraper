"""
Deterministic Runner — Orchestrates Mode A and Mode B execution flows.
Phase-based. No implicit agent interactions. Explicit escalation.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from shared.contracts.global_contract import AgentResult, AgentStatus
from shared.state.shared_state import (
    get_shared_state,
    ExecutionMode,
    GovernanceDecision,
    JobStatus,
    ScrapeJob,
)
from storage.filesystem_storage import get_storage
from planner.cost_aware_planner import CostAwarePlanner
from agents.ingestion.ingestion_agent import IngestionAgent, FetchPolicy
from agents.classical_scraper.classical_scraper_agent import ClassicalScraperAgent, FieldSchema
from agents.selector_repair.selector_repair_agent import SelectorRepairAgent
from agents.governance.governance_agent import GovernanceAgent


@dataclass
class RunResult:
    job_id: str
    mode: ExecutionMode
    success: bool
    record_count: int
    extraction_uri: Optional[str]
    governance_uri: Optional[str]
    tokens_used: int
    error: Optional[str] = None
    escalated: bool = False


class DeterministicRunner:
    """
    Orchestrates a full scrape job end-to-end.

    Mode A flow:
        Plan → Ingest → Extract → Done

    Mode B flow:
        Plan → Ingest → Extract (attempt) → [on failure] Repair → Governance →
        [if approved] Re-extract → Done

    Constraints (Global Contract §3, §4, §7):
    - Mode A never calls LLM
    - Mode B: single repair attempt, terminate after
    - Silent failure forbidden
    - All phases update shared state
    """

    def __init__(
        self,
        planner:   Optional[CostAwarePlanner]   = None,
        ingestion: Optional[IngestionAgent]     = None,
        scraper:   Optional[ClassicalScraperAgent] = None,
        repair:    Optional[SelectorRepairAgent] = None,
        governance: Optional[GovernanceAgent]   = None,
    ) -> None:
        self._state      = get_shared_state()
        self._storage    = get_storage()
        self._planner    = planner    or CostAwarePlanner()
        self._ingestion  = ingestion  or IngestionAgent()
        self._scraper    = scraper    or ClassicalScraperAgent()
        self._repair     = repair     or SelectorRepairAgent()
        self._governance = governance or GovernanceAgent()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        site_id: str,
        url: str,
        selectors: Dict[str, str],
        schema: Dict[str, FieldSchema],
        mode: ExecutionMode = ExecutionMode.A,
        item_container_selector: Optional[str] = None,
        token_allowance: Optional[int] = None,
    ) -> RunResult:
        """
        Execute a full scrape job.

        Args:
            site_id: Logical site identifier.
            url: Target URL.
            selectors: field_name → CSS selector.
            schema: field_name → FieldSchema.
            mode: ExecutionMode.A (deterministic) or B (LLM repair).
            item_container_selector: Optional list-item container.
            token_allowance: Optional token budget override for Mode B.

        Returns:
            RunResult summarising job outcome.
        """
        job_id = str(uuid.uuid4())[:8]

        # Register job
        job = ScrapeJob(
            job_id=job_id,
            site_id=site_id,
            url=url,
            mode=mode,
            status=JobStatus.RUNNING,
        )
        self._state.create_job(job)

        try:
            result = self._execute(
                job_id=job_id,
                url=url,
                selectors=selectors,
                schema=schema,
                mode=mode,
                item_container_selector=item_container_selector,
                token_allowance=token_allowance,
            )
        except Exception as exc:
            self._state.update_job(job_id, "runner", status=JobStatus.FAILURE, error=str(exc))
            return RunResult(
                job_id=job_id,
                mode=mode,
                success=False,
                record_count=0,
                extraction_uri=None,
                governance_uri=None,
                tokens_used=0,
                error=str(exc),
            )

        final_status = JobStatus.SUCCESS if result.success else JobStatus.FAILURE
        self._state.update_job(job_id, "runner", status=final_status)
        self._state.emit_metric("runner.job_completed", 1, {"job_id": job_id, "mode": mode.value})
        return result

    # ------------------------------------------------------------------
    # Internal flow
    # ------------------------------------------------------------------

    def _execute(
        self,
        job_id: str,
        url: str,
        selectors: Dict[str, str],
        schema: Dict[str, FieldSchema],
        mode: ExecutionMode,
        item_container_selector: Optional[str],
        token_allowance: Optional[int],
    ) -> RunResult:

        # ── Phase 1: Plan ──────────────────────────────────────────────
        plan = self._planner.plan(job_id, mode, token_allowance)

        # ── Phase 2: Ingest ────────────────────────────────────────────
        ingestion = self._ingestion.ingest(job_id, url)

        # ── Phase 3: Extract ───────────────────────────────────────────
        extraction = self._scraper.extract(
            job_id=job_id,
            dom_snapshot_uri=ingestion.dom_snapshot_uri,
            selectors=selectors,
            schema=schema,
            item_container_selector=item_container_selector,
        )

        # Mode A: done regardless of extraction success/failure
        if mode == ExecutionMode.A:
            return RunResult(
                job_id=job_id,
                mode=mode,
                success=extraction.success,
                record_count=extraction.record_count,
                extraction_uri=extraction.extraction_uri,
                governance_uri=None,
                tokens_used=0,
                error=extraction.error,
            )

        # ── Mode B: repair if extraction failed ────────────────────────
        if extraction.success:
            # Deterministic path succeeded — skip LLM
            return RunResult(
                job_id=job_id,
                mode=mode,
                success=True,
                record_count=extraction.record_count,
                extraction_uri=extraction.extraction_uri,
                governance_uri=None,
                tokens_used=0,
            )

        # Collect broken selectors and errors
        broken_selectors: Dict[str, str] = {}
        field_errors: Dict[str, str] = {}
        for record in extraction.records:
            for field, status in record.field_status.items():
                if status.value != "ok":
                    broken_selectors[field] = selectors.get(field, "")
                    field_errors[field] = record.field_errors.get(field, "unknown error")

        # ── Phase 4: Repair (single LLM call) ──────────────────────────
        repair_result = self._repair.propose_repair(
            job_id=job_id,
            dom_snapshot_uri=ingestion.dom_snapshot_uri,
            broken_selectors=broken_selectors,
            field_errors=field_errors,
            token_budget=plan.token_budget,
            planner_decision_ref=plan.decision_uri,
        )

        if not repair_result.success or not repair_result.proposal_uri:
            return RunResult(
                job_id=job_id,
                mode=mode,
                success=False,
                record_count=0,
                extraction_uri=extraction.extraction_uri,
                governance_uri=None,
                tokens_used=repair_result.tokens_used,
                error=repair_result.error or "Repair failed",
            )

        # ── Phase 5: Governance ────────────────────────────────────────
        gov_record = self._governance.review(
            job_id=job_id,
            proposal_uri=repair_result.proposal_uri,
            original_selectors=selectors,
        )

        if gov_record.decision != GovernanceDecision.APPROVED:
            return RunResult(
                job_id=job_id,
                mode=mode,
                success=False,
                record_count=0,
                extraction_uri=extraction.extraction_uri,
                governance_uri=gov_record.governance_uri,
                tokens_used=repair_result.tokens_used,
                error=f"Governance rejected: {gov_record.rejection_reason}",
            )

        # ── Phase 6: Re-extract with approved selectors ────────────────
        approved_selectors = {**selectors, **gov_record.proposed_selectors}
        re_extraction = self._scraper.extract(
            job_id=job_id,
            dom_snapshot_uri=ingestion.dom_snapshot_uri,
            selectors=approved_selectors,
            schema=schema,
            item_container_selector=item_container_selector,
        )

        return RunResult(
            job_id=job_id,
            mode=mode,
            success=re_extraction.success,
            record_count=re_extraction.record_count,
            extraction_uri=re_extraction.extraction_uri,
            governance_uri=gov_record.governance_uri,
            tokens_used=repair_result.tokens_used,
            error=re_extraction.error,
        )
