# A4: element-signature similarity scoring for selector relocation.
#
# A hash-based signature either matches or it doesn't — exactly the failure
# mode that made the original selector break. This scores a graded, redundant
# multi-signal match instead: tag, text, attributes, the identity-bearing
# class/id/href/src attributes counted again on their own, ancestor path,
# parent, and siblings. Dividing by the number of checks actually run (not a
# fixed denominator) means an element with no text or no parent isn't
# penalised for signals it never had.
#
# Signatures are plain JSON-safe dicts (lists, not tuples) so they persist on
# ExtractionOverlay.field_signatures without any round-trip conversion.

from difflib import SequenceMatcher
from typing import Any

DEFAULT_RELOCATION_THRESHOLD = 0.45


def _ratio(a: Any, b: Any) -> float:
    a = a if a is not None else ""
    b = b if b is not None else ""
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def score_similarity(original: dict[str, Any], candidate: dict[str, Any]) -> float:
    """Score in [0, 1]: score / checks over every signal both signatures
    carry data for."""
    score = 0.0
    checks = 0

    checks += 1
    score += 1.0 if original.get("tag") == candidate.get("tag") else 0.0

    orig_text = original.get("text") or ""
    if orig_text:
        checks += 1
        score += _ratio(orig_text, candidate.get("text"))

    orig_attrs: dict[str, str] = original.get("attrs") or {}
    cand_attrs: dict[str, str] = candidate.get("attrs") or {}
    checks += 1
    score += 0.5 * _ratio(sorted(orig_attrs.keys()), sorted(cand_attrs.keys()))
    score += 0.5 * _ratio(sorted(orig_attrs.values()), sorted(cand_attrs.values()))

    for key in ("class", "id", "href", "src"):
        checks += 1
        score += _ratio(orig_attrs.get(key), cand_attrs.get(key))

    checks += 1
    score += _ratio(original.get("ancestor_tags") or [], candidate.get("ancestor_tags") or [])

    if original.get("parent_tag") is not None:
        checks += 1
        score += 1.0 if original.get("parent_tag") == candidate.get("parent_tag") else 0.0
        checks += 1
        score += _ratio(
            sorted((original.get("parent_attrs") or {}).keys()),
            sorted((candidate.get("parent_attrs") or {}).keys()),
        )
        checks += 1
        score += _ratio(original.get("parent_text") or "", candidate.get("parent_text"))

    checks += 1
    score += _ratio(original.get("sibling_tags") or [], candidate.get("sibling_tags") or [])

    return score / checks if checks else 0.0


def find_best_relocation(
    original_signature: dict[str, Any],
    candidates: list[tuple[str, dict[str, Any]]],
    threshold: float = DEFAULT_RELOCATION_THRESHOLD,
) -> tuple[str, float] | None:
    """`candidates`: (candidate_id, signature) pairs. Returns the
    highest-scoring candidate at or above `threshold`, or None.

    Does not early-exit on a perfect score — ties are the normal case for
    list items sharing an identical shape. First-wins among ties, since
    candidates are expected in DOM order.
    """
    best_id: str | None = None
    best_score = -1.0
    for cand_id, sig in candidates:
        s = score_similarity(original_signature, sig)
        if s > best_score:
            best_score = s
            best_id = cand_id
    if best_id is not None and best_score >= threshold:
        return best_id, best_score
    return None
