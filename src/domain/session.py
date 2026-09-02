# P3: Session — persona + proxy bound together for a lease's lifetime, with
# a health score that only ever moves the pair towards retirement together.
# docs/plans/2026-08-13-capability-enhancement-plan.md P3.

from pydantic import BaseModel, Field

INITIAL_HEALTH_SCORE = 0.0
SUCCESS_DELTA = 1.0
BLOCK_DELTA = -3.0
RETIREMENT_THRESHOLD = -3.0
MAX_USES = 50


class Session(BaseModel):
    """Immutable — score_outcome/use_once return a new Session; the pool
    replaces its registry entry rather than mutating this in place."""

    session_id: str
    persona_id: str
    proxy: str | None = Field(default=None, description="Proxy URL, or None for direct connection.")
    health_score: float = Field(default=INITIAL_HEALTH_SCORE)
    uses: int = Field(default=0)

    @property
    def retired(self) -> bool:
        return self.health_score <= RETIREMENT_THRESHOLD or self.uses >= MAX_USES

    def score_outcome(self, *, success: bool, blocked: bool) -> "Session":
        delta = BLOCK_DELTA if blocked else (SUCCESS_DELTA if success else 0.0)
        return self.model_copy(update={
            "health_score": self.health_score + delta,
            "uses": self.uses + 1,
        })
