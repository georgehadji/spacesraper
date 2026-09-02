# P1: pure policy — should this domain's next fetch even attempt Tier 1
# (cheap HTTP), or has it already been learned to need a browser?
# docs/plans/2026-08-13-capability-enhancement-plan.md P1.

from src.domain.models import DomainProfile

BLOCK_RATE_ESCALATION_THRESHOLD = 0.3


def should_attempt_http_tier(profile: DomainProfile) -> bool:
    """False once a domain is known to need a browser — either directly
    (preferred_strategy == "browser", set by AdaptiveFetchService on a
    Tier-1 miss) or because its observed block rate is too high to bother."""
    if profile.preferred_strategy == "browser":
        return False
    return profile.block_rate < BLOCK_RATE_ESCALATION_THRESHOLD
