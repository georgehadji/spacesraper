"""
Classical Scraper Agent — Deterministic Extraction (Mode A fast-path).
CSS/XPath selector application with field-level validation.
No LLM, no retries, no fallback.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from shared.contracts.global_contract import BaseAgent, AgentResult, AgentStatus
from shared.state.shared_state import get_shared_state
from storage.filesystem_storage import get_storage


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class FieldType(Enum):
    STRING = "string"
    NUMBER = "number"
    ARRAY  = "array"
    OBJECT = "object"


class FieldStatus(Enum):
    OK             = "ok"
    MISSING        = "missing"
    EMPTY          = "empty"
    TYPE_MISMATCH  = "type_mismatch"


@dataclass
class FieldSchema:
    field_type: FieldType = FieldType.STRING
    required: bool = True


@dataclass
class ExtractionRecord:
    data: Dict[str, Any]
    field_status: Dict[str, FieldStatus]
    field_errors: Dict[str, str]


@dataclass
class ExtractionResult:
    job_id: str
    records: List[ExtractionRecord]
    extraction_uri: str
    extracted_at: float
    record_count: int
    success: bool
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Selector engine (deterministic, no external deps)
# ---------------------------------------------------------------------------

class _SelectorEngine:
    """
    Lightweight deterministic CSS selector engine over dict-based DOM snapshots.
    Supports: tag, .class, #id, [attr], tag.class
    """

    def select(self, dom: Dict[str, Any], selector: str) -> List[Any]:
        results: List[Any] = []
        self._walk(dom, selector, results)
        return results

    def select_one(self, dom: Dict[str, Any], selector: str) -> Optional[Any]:
        found = self.select(dom, selector)
        return found[0] if found else None

    def _walk(self, node: Any, selector: str, acc: List[Any]) -> None:
        if not isinstance(node, dict):
            return
        if self._matches(node, selector):
            acc.append(node)
        for child in node.get("children", []):
            self._walk(child, selector, acc)

    def _matches(self, node: Dict[str, Any], selector: str) -> bool:
        tag    = node.get("tag", "")
        cls    = node.get("class", "")
        nid    = node.get("id", "")
        attrs  = node.get("attrs", {})

        # .class
        if selector.startswith("."):
            return selector[1:] in cls.split()
        # #id
        if selector.startswith("#"):
            return nid == selector[1:]
        # [attr=val] or [attr]
        m = re.match(r'\[(\w+)(?:=["\']?(.*?)["\']?)?\]$', selector)
        if m:
            attr_name, attr_val = m.group(1), m.group(2)
            if attr_name not in attrs:
                return False
            return (attr_val is None) or (attrs[attr_name] == attr_val)
        # tag.class
        if "." in selector:
            parts = selector.split(".", 1)
            return tag == parts[0] and parts[1] in cls.split()
        # plain tag
        return tag == selector

    @staticmethod
    def get_text(node: Any) -> str:
        if isinstance(node, str):
            return node.strip()
        if isinstance(node, dict):
            return node.get("text", "").strip()
        return ""

    @staticmethod
    def get_attr(node: Any, attr: str) -> str:
        if isinstance(node, dict):
            return node.get("attrs", {}).get(attr, "")
        return ""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class ClassicalScraperAgent(BaseAgent):
    """
    Applies deterministic selectors to a stored DOM snapshot.
    Returns structured records with per-field status.

    Constraints (Global Contract):
    - No LLM
    - No retries
    - Silent failure forbidden
    - Deterministic: same DOM + selectors → same records
    """

    AGENT_NAME = "classical_scraper_agent"

    def __init__(self) -> None:
        self._state   = get_shared_state()
        self._storage = get_storage()
        self._engine  = _SelectorEngine()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(
        self,
        job_id: str,
        dom_snapshot_uri: str,
        selectors: Dict[str, str],
        schema: Dict[str, FieldSchema],
        item_container_selector: Optional[str] = None,
    ) -> ExtractionResult:
        """
        Apply selectors to stored DOM and return structured records.

        Args:
            job_id: Owning job identifier.
            dom_snapshot_uri: URI of stored DOM snapshot (JSON dict format).
            selectors: field_name → CSS selector mapping.
            schema: field_name → FieldSchema for validation.
            item_container_selector: If set, extract one record per matching container.

        Returns:
            ExtractionResult with records and extraction_uri.

        Raises:
            ValueError: Invalid inputs.
            RuntimeError: Storage or parsing failure.
        """
        if not job_id:
            raise ValueError("job_id required")
        if not dom_snapshot_uri:
            raise ValueError("dom_snapshot_uri required")
        if not selectors:
            raise ValueError("selectors required")

        # Load DOM from storage
        try:
            dom = self._storage.read_json(dom_snapshot_uri)
        except Exception as exc:
            raise RuntimeError(f"Cannot read DOM snapshot: {exc}") from exc

        # Extract records
        if item_container_selector:
            containers = self._engine.select(dom, item_container_selector)
            if not containers:
                containers = [dom]  # fallback: treat whole doc as single item
        else:
            containers = [dom]

        records: List[ExtractionRecord] = []
        for container in containers:
            record = self._extract_record(container, selectors, schema)
            records.append(record)

        # Persist extraction result
        result_data = {
            "job_id": job_id,
            "records": [
                {"data": r.data,
                 "field_status": {k: v.value for k, v in r.field_status.items()},
                 "field_errors": r.field_errors}
                for r in records
            ],
            "record_count": len(records),
            "extracted_at": time.time(),
        }
        uri = self._storage.write_json(result_data, f"extraction_{job_id}.json")

        self._state.update_job(job_id, self.AGENT_NAME, extraction_result_uri=uri)
        self._state.emit_metric("extraction.records", len(records), {"job_id": job_id})

        success = all(
            all(s in (FieldStatus.OK,) for s in r.field_status.values())
            for r in records
        )

        return ExtractionResult(
            job_id=job_id,
            records=records,
            extraction_uri=uri,
            extracted_at=result_data["extracted_at"],
            record_count=len(records),
            success=success,
        )

    def execute(self, **kwargs: Any) -> AgentResult:
        """BaseAgent protocol wrapper."""
        try:
            result = self.extract(
                job_id=kwargs["job_id"],
                dom_snapshot_uri=kwargs["dom_snapshot_uri"],
                selectors=kwargs["selectors"],
                schema=kwargs.get("schema", {}),
                item_container_selector=kwargs.get("item_container_selector"),
            )
            return AgentResult(
                status=AgentStatus.SUCCESS if result.success else AgentStatus.PARTIAL,
                data={"extraction_uri": result.extraction_uri, "record_count": result.record_count},
                metadata={"success": result.success},
            )
        except (ValueError, RuntimeError) as exc:
            return AgentResult(status=AgentStatus.FAILURE, data={}, error=str(exc))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _extract_record(
        self,
        dom: Dict[str, Any],
        selectors: Dict[str, str],
        schema: Dict[str, FieldSchema],
    ) -> ExtractionRecord:
        data: Dict[str, Any] = {}
        field_status: Dict[str, FieldStatus] = {}
        field_errors: Dict[str, str] = {}

        for field_name, selector in selectors.items():
            fs = schema.get(field_name, FieldSchema())
            node = self._engine.select_one(dom, selector)

            if node is None:
                data[field_name] = None
                if fs.required:
                    field_status[field_name] = FieldStatus.MISSING
                    field_errors[field_name] = f"Required field not found: {selector}"
                else:
                    field_status[field_name] = FieldStatus.MISSING
                continue

            value = self._engine.get_text(node)

            if not value and fs.required:
                data[field_name] = value
                field_status[field_name] = FieldStatus.EMPTY
                field_errors[field_name] = f"Required field is empty: {field_name}"
            else:
                data[field_name] = value
                field_status[field_name] = FieldStatus.OK

        return ExtractionRecord(data=data, field_status=field_status, field_errors=field_errors)
