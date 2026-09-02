# A1: BlockSignalDetector — generalizes the old four-title-string check into
# a pure function shared by both fetch tiers (browser and turbo/httpx).
# Detection only; solving a challenge stays a non-goal (08-13 plan §6).

from dataclasses import dataclass

BLOCK_STATUS_CODES = (403, 429, 503)

_CHALLENGE_TITLE_MARKERS = (
    "Just a moment...",
    "Attention Required",
    "Access Denied",
    "Checking your browser",
)

# Cloudflare Turnstile / managed-challenge markers, as they appear in the raw
# HTML of a challenge page — cType is the config key Cloudflare's own JS
# reads to decide how to render the widget.
_CHALLENGE_BODY_MARKERS = (
    '"cType":"managed"',
    '"cType":"interactive"',
    '"cType":"non-interactive"',
    "challenges.cloudflare.com/turnstile",
)


@dataclass(frozen=True)
class BlockSignal:
    blocked: bool
    reason: str | None = None


def detect_block(
    *,
    status_code: int | None = None,
    title: str | None = None,
    body_sample: str | None = None,
) -> BlockSignal:
    """
    Check the cheap, stateless signals available to either fetch tier.

    Deliberately does NOT include the content-length-collapse-vs-domain-median
    signal from the plan: that needs a persisted rolling window per domain,
    which is a real schema addition, not a pure-function concern — left for
    whoever wires per-domain observation storage for it.
    """
    if status_code in BLOCK_STATUS_CODES:
        return BlockSignal(True, f"status {status_code}")

    if title and any(marker in title for marker in _CHALLENGE_TITLE_MARKERS):
        return BlockSignal(True, f"challenge title: {title!r}")

    if body_sample and any(marker in body_sample for marker in _CHALLENGE_BODY_MARKERS):
        return BlockSignal(True, "challenge markers in response body")

    return BlockSignal(False, None)
