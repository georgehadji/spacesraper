"""
Task 5.2 — Tests for pure LLM output quality metric functions.
Table-testable: no I/O, no mocking required.
"""

import pytest

from src.application.llm_metrics import groundedness, citation_coverage


class TestGroundedness:
    def test_no_claims_is_vacuously_grounded(self):
        assert groundedness([], ["some source text"]) == 1.0
        assert groundedness([], []) == 1.0

    def test_claims_with_no_sources_are_ungrounded(self):
        assert groundedness(["the sky is blue"], []) == 0.0

    def test_fully_grounded_claim(self):
        sources = ["The widget costs 9.99 dollars and ships from Berlin."]
        claims = ["The widget costs 9.99 dollars."]
        assert groundedness(claims, sources) == 1.0

    def test_fully_ungrounded_claim(self):
        sources = ["The widget costs 9.99 dollars and ships from Berlin."]
        claims = ["The rocket launches from Florida next Tuesday."]
        assert groundedness(claims, sources) == 0.0

    def test_partial_grounding_across_multiple_claims(self):
        sources = ["The widget costs 9.99 dollars and ships from Berlin."]
        claims = [
            "The widget costs 9.99 dollars.",  # grounded
            "The rocket launches next Tuesday.",  # not grounded
        ]
        assert groundedness(claims, sources) == 0.5

    def test_grounded_across_multiple_sources(self):
        sources = ["Record A: title Widget", "Record B: price 9.99"]
        claims = ["Widget costs 9.99"]
        result = groundedness(claims, sources)
        assert result == 1.0

    def test_empty_string_claim_does_not_count_toward_denominator(self):
        sources = ["some content"]
        claims = ["", "some content here"]
        # Empty claim is skipped; only 1 real claim, which is grounded
        assert groundedness(claims, sources) == 1.0

    def test_result_always_in_unit_interval(self):
        sources = ["alpha beta gamma"]
        for claims in [[], ["alpha"], ["delta"], ["alpha", "delta"], ["alpha beta gamma delta epsilon"]]:
            result = groundedness(claims, sources)
            assert 0.0 <= result <= 1.0


class TestCitationCoverage:
    def test_empty_answer_is_vacuously_covered(self):
        assert citation_coverage("") == 1.0
        assert citation_coverage("   ") == 1.0

    def test_fully_cited_answer(self):
        # Convention: the citation sits inside the sentence, before the
        # terminal punctuation, so it doesn't split off as its own segment.
        answer = "The widget is popular [[record:rec_1]]. It ships fast [[record:rec_2]]."
        assert citation_coverage(answer) == 1.0

    def test_fully_uncited_answer(self):
        answer = "The widget is popular. It ships fast."
        assert citation_coverage(answer) == 0.0

    def test_partial_citation(self):
        answer = "The widget is popular [[record:rec_1]]. It ships fast."
        assert citation_coverage(answer) == 0.5

    def test_single_sentence_with_citation(self):
        answer = "The widget costs 9.99 dollars [[record:rec_42]]."
        assert citation_coverage(answer) == 1.0

    def test_result_always_in_unit_interval(self):
        answers = [
            "One sentence.",
            "One sentence. [[record:a]]",
            "A. B. C. [[record:x]]",
            "",
        ]
        for answer in answers:
            result = citation_coverage(answer)
            assert 0.0 <= result <= 1.0
