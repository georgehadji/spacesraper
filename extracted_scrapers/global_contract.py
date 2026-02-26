"""
Global Contract — Agent Protocol
Defines base classes and result types for all agents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class AgentStatus(Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"


@dataclass
class AgentResult:
    status: AgentStatus
    data: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


class BaseAgent:
    """All agents extend this. Single responsibility enforced by contract."""

    AGENT_NAME: str = "base"

    def execute(self, **kwargs: Any) -> AgentResult:
        raise NotImplementedError

    def health_check(self) -> bool:
        return True
