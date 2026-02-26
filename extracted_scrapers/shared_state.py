"""
Shared State — Single Source of Truth
Append-only, versioned, audit-traceable.
All agent communication passes through here.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class JobStatus(Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    SUCCESS   = "success"
    FAILURE   = "failure"
    ESCALATED = "escalated"


class ExecutionMode(Enum):
    A = "A"   # deterministic fast-path
    B = "B"   # LLM-powered selector repair


class GovernanceDecision(Enum):
    PENDING  = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# ---------------------------------------------------------------------------
# Core entities
# ---------------------------------------------------------------------------

@dataclass
class ScrapeJob:
    job_id: str
    site_id: str
    url: str
    mode: ExecutionMode
    status: JobStatus = JobStatus.PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: Optional[str] = None
    dom_snapshot_uri: Optional[str] = None
    extraction_result_uri: Optional[str] = None
    repair_proposal_uri: Optional[str] = None
    governance_decision: GovernanceDecision = GovernanceDecision.PENDING
    planner_decision_ref: Optional[str] = None
    token_budget: int = 0
    tokens_used: int = 0
    llm_invoked: bool = False
    repair_approved: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditEntry:
    timestamp: float
    job_id: str
    agent: str
    event: str
    data: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Shared state singleton
# ---------------------------------------------------------------------------

class SharedState:
    """
    Thread-safe, append-only shared state.
    Agents READ and WRITE exclusively through this interface.
    """

    _instance: Optional["SharedState"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "SharedState":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._jobs: Dict[str, ScrapeJob] = {}
                cls._instance._audit_log: List[AuditEntry] = []
                cls._instance._metrics: List[Dict[str, Any]] = []
        return cls._instance

    # --- Jobs ---

    def create_job(self, job: ScrapeJob) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
            self._append_audit(job.job_id, "SharedState", "job_created", {"mode": job.mode.value})

    def get_job(self, job_id: str) -> Optional[ScrapeJob]:
        return self._jobs.get(job_id)

    def update_job(self, job_id: str, agent: str, **kwargs: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"Job not found: {job_id}")
            for k, v in kwargs.items():
                setattr(job, k, v)
            job.updated_at = time.time()
            self._append_audit(job_id, agent, "job_updated", kwargs)

    def list_jobs(self) -> List[ScrapeJob]:
        return list(self._jobs.values())

    # --- Audit ---

    def _append_audit(self, job_id: str, agent: str, event: str, data: Dict[str, Any]) -> None:
        """Internal — always called under lock."""
        self._audit_log.append(AuditEntry(
            timestamp=time.time(),
            job_id=job_id,
            agent=agent,
            event=event,
            data={k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
                  for k, v in data.items()}
        ))

    def get_audit_log(self, job_id: Optional[str] = None) -> List[AuditEntry]:
        if job_id:
            return [e for e in self._audit_log if e.job_id == job_id]
        return list(self._audit_log)

    # --- Metrics ---

    def emit_metric(self, name: str, value: Any, labels: Optional[Dict[str, str]] = None) -> None:
        with self._lock:
            self._metrics.append({
                "name": name,
                "value": value,
                "labels": labels or {},
                "timestamp": time.time()
            })

    def get_metrics(self) -> List[Dict[str, Any]]:
        return list(self._metrics)

    # --- Reset (test use only) ---

    def _reset(self) -> None:
        with self._lock:
            self._jobs.clear()
            self._audit_log.clear()
            self._metrics.clear()


def get_shared_state() -> SharedState:
    return SharedState()
