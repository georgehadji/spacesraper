# P1 adaptive fetch tiering — tier-agnostic request/result shape shared by
# ImpersonatingHttpFetcher (Tier 1) and StealthBrowserFetcher (Tier 2).
# docs/plans/2026-08-13-capability-enhancement-plan.md P1.

from pydantic import BaseModel, Field


class FetchRequest(BaseModel):
    url: str
    timeout_s: float = Field(default=20.0, description="Per-fetch timeout in seconds.")


class FetchResult(BaseModel):
    """Outcome of one fetch attempt, from either tier."""

    url: str
    status_code: int = Field(default=0, description="HTTP status code, or 0 if the request never completed.")
    html: str = Field(default="", description="Response body / rendered DOM.")
    tier_used: str = Field(..., description="'http' or 'browser'.")
    blocked: bool = Field(default=False, description="A BlockSignalDetector hit fired on this response.")
    block_reason: str | None = Field(default=None)
    latency_ms: float = Field(default=0.0)
    retry_after_s: float | None = Field(default=None)
    error: str | None = Field(default=None, description="Transport/navigation error, if the fetch failed outright.")

    @property
    def ok(self) -> bool:
        """A usable, unblocked 2xx response with a real body."""
        return self.error is None and not self.blocked and 200 <= self.status_code < 300 and bool(self.html)
