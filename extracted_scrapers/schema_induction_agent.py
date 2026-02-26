"""
Schema Induction Agent — Onboarding Workflow for New Sites.
Infers selector candidates from a sample DOM. Onboarding-only, not used in production scraping.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from shared.contracts.global_contract import BaseAgent, AgentResult, AgentStatus
from shared.state.shared_state import get_shared_state
from storage.filesystem_storage import get_storage


@dataclass
class SelectorCandidate:
    field_name: str
    selector: str
    sample_value: str
    confidence: float


@dataclass
class InducedSchema:
    site_id: str
    candidates: List[SelectorCandidate]
    schema_uri: str
    induced_at: float


class SchemaInductionAgent(BaseAgent):
    """
    Analyzes sample DOM to propose selector candidates for a new site.
    Onboarding-only. Does not modify production configuration.

    Heuristics (deterministic, no LLM):
    - title: h1, title, [class*=title], [class*=heading]
    - price: [class*=price], [class*=cost], [class*=amount]
    - description: [class*=desc], [class*=summary], p
    - image: img (src attribute)
    """

    AGENT_NAME = "schema_induction_agent"

    HEURISTICS: Dict[str, List[str]] = {
        "title":       ["h1", ".title", ".heading", "[class*=title]"],
        "price":       [".price", "[class*=price]", "[class*=cost]"],
        "description": [".description", "[class*=desc]", "[class*=summary]", "p"],
        "image":       ["img"],
        "link":        ["a"],
    }

    def __init__(self) -> None:
        self._state   = get_shared_state()
        self._storage = get_storage()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def induce(self, site_id: str, dom_snapshot_uri: str) -> InducedSchema:
        """
        Infer selector candidates from stored DOM.

        Args:
            site_id: Logical site identifier.
            dom_snapshot_uri: URI of sample DOM snapshot.

        Returns:
            InducedSchema with candidates and schema_uri.
        """
        if not site_id:
            raise ValueError("site_id required")
        if not dom_snapshot_uri:
            raise ValueError("dom_snapshot_uri required")

        try:
            dom = self._storage.read_json(dom_snapshot_uri)
        except Exception as exc:
            raise RuntimeError(f"Cannot read DOM: {exc}") from exc

        candidates = self._infer_candidates(dom)

        schema_data = {
            "site_id":    site_id,
            "candidates": [
                {
                    "field_name":  c.field_name,
                    "selector":    c.selector,
                    "sample_value": c.sample_value,
                    "confidence":  c.confidence,
                }
                for c in candidates
            ],
            "induced_at": time.time(),
        }
        uri = self._storage.write_json(schema_data, f"schema_{site_id}.json")
        self._state.emit_metric("schema_induction.candidates", len(candidates), {"site_id": site_id})

        return InducedSchema(
            site_id=site_id,
            candidates=candidates,
            schema_uri=uri,
            induced_at=schema_data["induced_at"],
        )

    def execute(self, **kwargs: Any) -> AgentResult:
        try:
            result = self.induce(
                site_id=kwargs["site_id"],
                dom_snapshot_uri=kwargs["dom_snapshot_uri"],
            )
            return AgentResult(
                status=AgentStatus.SUCCESS,
                data={"schema_uri": result.schema_uri, "candidate_count": len(result.candidates)},
            )
        except (ValueError, RuntimeError) as exc:
            return AgentResult(status=AgentStatus.FAILURE, data={}, error=str(exc))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _infer_candidates(self, dom: Dict[str, Any]) -> List[SelectorCandidate]:
        candidates: List[SelectorCandidate] = []
        for field_name, selectors in self.HEURISTICS.items():
            for selector in selectors:
                sample = self._find_sample(dom, selector)
                if sample is not None:
                    candidates.append(SelectorCandidate(
                        field_name=field_name,
                        selector=selector,
                        sample_value=sample[:100],
                        confidence=0.7,
                    ))
                    break  # first match wins
        return candidates

    def _find_sample(self, node: Any, selector: str) -> Optional[str]:
        if not isinstance(node, dict):
            return None
        if self._matches(node, selector):
            return node.get("text", "") or node.get("attrs", {}).get("src", "")
        for child in node.get("children", []):
            result = self._find_sample(child, selector)
            if result is not None:
                return result
        return None

    def _matches(self, node: Dict[str, Any], selector: str) -> bool:
        tag = node.get("tag", "")
        cls = node.get("class", "")
        if selector.startswith("."):
            return selector[1:] in cls.split()
        if selector.startswith("[class*="):
            substr = selector[8:].rstrip("]").strip("'\"")
            return substr in cls
        return tag == selector
