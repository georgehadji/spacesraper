# A4: element-signature relocation — pure scorer tests plus the plan's own
# acceptance fixture (redesigned page, ≥80% field recovery, zero LLM calls).

import pytest

from src.application.extraction_pipeline import DeterministicExtractionPipeline, _build_element_signature
from src.domain.models import ExtractionOverlay
from src.domain.similarity import find_best_relocation, score_similarity
from scrapling import Selector


def test_identical_signature_scores_near_one():
    sig = {
        "tag": "span", "attrs": {"class": "price"}, "text": "$10",
        "ancestor_tags": ["div", "body", "html"], "parent_tag": "div",
        "parent_attrs": {"class": "card"}, "parent_text": "$10",
        "sibling_tags": ["h2", "span"], "child_tags": [],
    }
    assert score_similarity(sig, sig) == pytest.approx(1.0)


def test_completely_different_element_scores_low():
    original = {
        "tag": "span", "attrs": {"class": "price"}, "text": "$10",
        "ancestor_tags": ["div", "body", "html"], "parent_tag": "div",
        "parent_attrs": {"class": "card"}, "parent_text": "$10",
        "sibling_tags": ["h2", "span"], "child_tags": [],
    }
    candidate = {
        "tag": "footer", "attrs": {}, "text": "Copyright 2026",
        "ancestor_tags": ["body", "html"], "parent_tag": "html",
        "parent_attrs": {}, "parent_text": "Copyright 2026",
        "sibling_tags": ["nav"], "child_tags": [],
    }
    assert score_similarity(original, candidate) < 0.45


def test_find_best_relocation_picks_highest_scoring_candidate():
    original = {"tag": "span", "attrs": {"class": "price"}, "text": "$10", "ancestor_tags": [],
                "parent_tag": None, "parent_attrs": {}, "parent_text": "", "sibling_tags": [], "child_tags": []}
    close = {"tag": "span", "attrs": {"class": "cost"}, "text": "$10", "ancestor_tags": [],
             "parent_tag": None, "parent_attrs": {}, "parent_text": "", "sibling_tags": [], "child_tags": []}
    far = {"tag": "div", "attrs": {}, "text": "unrelated", "ancestor_tags": [],
           "parent_tag": None, "parent_attrs": {}, "parent_text": "", "sibling_tags": [], "child_tags": []}
    result = find_best_relocation(original, [("far", far), ("close", close)])
    assert result is not None
    assert result[0] == "close"


ORIGINAL_HTML = """
<div id="results">
  <div class="product-card">
    <h2 class="product-title">Wireless Mouse</h2>
    <span class="product-price">$25.99</span>
    <p class="product-desc">Ergonomic wireless mouse</p>
  </div>
</div>
"""

# Same container id, everything inside redesigned: classes renamed, order
# shuffled. This is exactly the "selector breaks, page didn't really change"
# case A4 exists for.
REDESIGNED_HTML = """
<div id="results">
  <div class="item-v2">
    <span class="tag-cost">$25.99</span>
    <h2 class="tag-heading">Wireless Mouse</h2>
    <p class="tag-summary">Ergonomic wireless mouse</p>
  </div>
</div>
"""


class FakeOverlayRepoWithActive:
    def __init__(self, active: ExtractionOverlay):
        self._active = active
        self.saved = []

    async def create_overlay(self, overlay: ExtractionOverlay) -> ExtractionOverlay:
        self.saved.append(overlay)
        return overlay

    async def get_active_overlay(self, domain: str):
        return self._active if domain == self._active.domain else None


def _captured_overlay() -> ExtractionOverlay:
    original = Selector(ORIGINAL_HTML)
    card = original.css(".product-card").first
    title_el = card.css("h2.product-title").first
    price_el = card.css("span.product-price").first
    desc_el = card.css("p.product-desc").first
    return ExtractionOverlay(
        overlay_id="ovl_original",
        domain="example.com",
        schema_id="test_schema",
        state="ACTIVE",
        container_selector="#results",
        field_mappings={
            "title": "h2.product-title",
            "price": "span.product-price",
            "desc": "p.product-desc",
        },
        field_signatures={
            "title": _build_element_signature(title_el),
            "price": _build_element_signature(price_el),
            "desc": _build_element_signature(desc_el),
        },
    )


@pytest.mark.asyncio
async def test_relocation_recovers_fields_after_redesign_with_zero_llm_calls():
    overlay = _captured_overlay()
    repo = FakeOverlayRepoWithActive(overlay)
    pipeline = DeterministicExtractionPipeline(overlay_repo=repo)

    records = await pipeline.extract(REDESIGNED_HTML, [], "https://example.com/product")

    assert len(records) == 1
    data = records[0].data
    recovered = sum(1 for f in ("title", "price", "desc") if data.get(f))
    assert recovered / 3 >= 0.8, f"only recovered {data!r}"
    assert data["title"] == "Wireless Mouse"
    assert data["price"] == "$25.99"
    assert data["desc"] == "Ergonomic wireless mouse"

    # A new CANDIDATE overlay was proposed with regenerated selectors —
    # never mutating the ACTIVE one directly.
    assert len(repo.saved) == 1
    proposed = repo.saved[0]
    assert proposed.rollback_overlay_id == "ovl_original"
    assert proposed.state.value == "CANDIDATE"
