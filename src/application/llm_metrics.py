# Pure metric functions for scoring LLM output quality (Phase 5, Task 5.2).
# No I/O, table-testable — the evaluation loop (StrategyObservation ->
# StrategyEvaluator -> EvaluationResult) already exists; it was missing a
# metric for the LLM's own output, not a framework. See llm_extract wiring
# in application/pipeline.py and (Phase 6) SynthesisService for llm_synthesis.

import re
from typing import List, Set

_WORD_RE = re.compile(r"[a-z0-9]+")
_CITATION_RE = re.compile(r"\[\[record:[^\]]+\]\]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# A claim counts as grounded if at least this fraction of its tokens appear
# in the combined source text. A simple token-overlap baseline; the plan
# allows an optional embedding-similarity refinement, deliberately not
# built here since that requires a network call and this module is I/O-free.
_GROUNDED_OVERLAP_THRESHOLD = 0.5


def _tokenize(text: str) -> Set[str]:
    return set(_WORD_RE.findall(text.lower()))


def groundedness(claims: List[str], sources: List[str]) -> float:
    """
    Fraction of `claims` traceable to `sources`, by token overlap.

    A claim is "grounded" when at least half of its tokens appear in the
    combined source text (record fields or SERP snippets — always passed as
    data, never as a source of instructions).

    Returns:
        1.0 if there are no claims (vacuously grounded — nothing false was said).
        0.0 if there are claims but no sources to ground them in.
        Otherwise, grounded_claim_count / total_claim_count, in [0, 1].
    """
    if not claims:
        return 1.0
    if not sources:
        return 0.0

    source_tokens: Set[str] = set()
    for source in sources:
        source_tokens |= _tokenize(source)

    grounded = 0
    counted = 0
    for claim in claims:
        claim_tokens = _tokenize(claim)
        if not claim_tokens:
            continue
        counted += 1
        overlap = len(claim_tokens & source_tokens) / len(claim_tokens)
        if overlap >= _GROUNDED_OVERLAP_THRESHOLD:
            grounded += 1

    if counted == 0:
        return 1.0
    return grounded / counted


def citation_coverage(answer: str) -> float:
    """
    Fraction of sentences in `answer` carrying a record citation.

    Citation convention: `[[record:<record_id>]]` placed inside the sentence,
    before its closing punctuation — e.g. "Cost is 9.99 [[record:rec_1]]." —
    so the marker is not itself mistaken for a sentence boundary. This is the
    convention SynthesisService (Phase 6) is expected to emit. Uncited claims
    are dropped before publication there; this metric is what makes that
    mandate measurable.

    Returns 1.0 for an empty answer (vacuously covered).
    """
    if not answer or not answer.strip():
        return 1.0

    sentences = [s for s in _SENTENCE_SPLIT_RE.split(answer.strip()) if s.strip()]
    if not sentences:
        return 1.0

    cited = sum(1 for s in sentences if _CITATION_RE.search(s))
    return cited / len(sentences)
