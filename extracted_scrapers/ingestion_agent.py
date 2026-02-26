"""
Ingestion Agent — Fetch and store raw HTML snapshots.
Responsibility: HTTP fetch → DOM snapshot → storage URI.
No parsing, no LLM, no selector logic.
"""

from __future__ import annotations

import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from shared.contracts.global_contract import BaseAgent, AgentResult, AgentStatus
from shared.state.shared_state import get_shared_state
from storage.filesystem_storage import get_storage


class FetchPolicy(Enum):
    STATIC        = "static"         # plain HTTP GET
    RENDER_REQUIRED = "render_required"  # JS rendering (mocked)


@dataclass
class IngestionResult:
    job_id: str
    url: str
    dom_snapshot_uri: str
    fetch_policy: FetchPolicy
    status_code: int
    fetched_at: float
    content_length: int
    error: Optional[str] = None


class IngestionAgent(BaseAgent):
    """
    Fetches a URL and persists the DOM snapshot to storage.
    Returns a URI referencing the snapshot — no content is passed in memory.

    Constraints (Global Contract):
    - No LLM usage
    - Deterministic: same URL + policy → stored snapshot
    - Silent failure forbidden: all errors explicit
    """

    AGENT_NAME = "ingestion_agent"

    def __init__(self, timeout_seconds: int = 10) -> None:
        self._timeout = timeout_seconds
        self._state  = get_shared_state()
        self._storage = get_storage()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest(self, job_id: str, url: str, fetch_policy: FetchPolicy = FetchPolicy.STATIC) -> IngestionResult:
        """
        Fetch URL and persist DOM snapshot.

        Args:
            job_id: Owning job identifier.
            url: Target URL to fetch.
            fetch_policy: STATIC or RENDER_REQUIRED.

        Returns:
            IngestionResult with dom_snapshot_uri.

        Raises:
            ValueError: Invalid inputs.
            RuntimeError: Fetch failure.
        """
        if not job_id:
            raise ValueError("job_id is required")
        if not url:
            raise ValueError("url is required")

        self._state.update_job(job_id, self.AGENT_NAME, status_code="fetching")

        try:
            html, status_code = self._fetch(url, fetch_policy)
        except Exception as exc:
            self._state.update_job(job_id, self.AGENT_NAME, error=str(exc))
            raise RuntimeError(f"Ingestion failed for {url}: {exc}") from exc

        uri = self._storage.write(html, f"dom_{job_id}.html")

        self._state.update_job(
            job_id,
            self.AGENT_NAME,
            dom_snapshot_uri=uri,
        )
        self._state.emit_metric("ingestion.snapshot_stored", 1, {"job_id": job_id})

        return IngestionResult(
            job_id=job_id,
            url=url,
            dom_snapshot_uri=uri,
            fetch_policy=fetch_policy,
            status_code=status_code,
            fetched_at=time.time(),
            content_length=len(html),
        )

    def execute(self, **kwargs: Any) -> AgentResult:
        """BaseAgent protocol wrapper."""
        try:
            result = self.ingest(
                job_id=kwargs["job_id"],
                url=kwargs["url"],
                fetch_policy=kwargs.get("fetch_policy", FetchPolicy.STATIC),
            )
            return AgentResult(
                status=AgentStatus.SUCCESS,
                data={"dom_snapshot_uri": result.dom_snapshot_uri},
                metadata={"status_code": result.status_code, "content_length": result.content_length},
            )
        except (ValueError, RuntimeError) as exc:
            return AgentResult(
                status=AgentStatus.FAILURE,
                data={},
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fetch(self, url: str, policy: FetchPolicy) -> tuple[str, int]:
        """Fetch HTML. RENDER_REQUIRED uses same GET (mock: no headless browser)."""
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "SelfHealingScraper/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                html = resp.read().decode("utf-8", errors="replace")
                return html, resp.status
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"URL error: {exc.reason}") from exc
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc
