"""P6 — one malformed JSON-LD item must not discard its siblings (D29).

JSON-LD permits @type as either a string or an array of strings. The record
builder called .lower() on it unconditionally, so an array raised
AttributeError — and that except sat at *block* scope, around the whole
<script> tag, so every remaining valid item in the same block was thrown
away with it. A page whose first product carries `"@type": ["Product",
"Offer"]` lost every product after it.
"""

import json

import pytest
from scrapling import Selector

from src.application.extraction_pipeline import DeterministicExtractionPipeline

URL = "https://example.invalid/catalogue"


def page(*blocks: object) -> Selector:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in blocks
    )
    return Selector(f"<html><body>{scripts}</body></html>")


def pipeline() -> DeterministicExtractionPipeline:
    return DeterministicExtractionPipeline()


def test_a_a_list_valued_type_does_not_discard_the_rest_of_its_block():
    """The guard for D29."""
    records = pipeline()._extract_json_ld(
        page(
            [
                {"@type": ["Product", "Offer"], "name": "First"},
                {"@type": "Product", "name": "Second"},
                {"@type": "Product", "name": "Third"},
            ]
        ),
        URL,
    )

    names = [r.data.get("name") for r in records]
    assert names == ["First", "Second", "Third"], (
        "one item with an array @type discarded the valid items after it"
    )


def test_b_a_list_valued_type_is_recorded_as_a_usable_type():
    """The array's first string entry is the record's type, not 'list'."""
    records = pipeline()._extract_json_ld(
        page({"@type": ["MedicalBusiness", "LocalBusiness"], "name": "Clinic"}), URL
    )

    assert len(records) == 1
    assert records[0].record_type == "medicalbusiness"


def test_c_one_unusable_item_does_not_discard_its_siblings(monkeypatch):
    """The narrowed except scope itself, independent of what triggers it.

    Handling list-valued @type fixes the one shape we know about. This pins
    the other half of the finding: whatever the next unusable item turns out
    to be, it must cost its own record and not the rest of its block.
    """
    pipe = pipeline()
    real = pipe._make_json_ld_record

    def explode(item, current_url):
        if item.get("name") == "Broken":
            raise AttributeError("simulated unusable item")
        return real(item, current_url)

    monkeypatch.setattr(pipe, "_make_json_ld_record", explode)

    records = pipe._extract_json_ld(
        page(
            [
                {"@type": "Product", "name": "Before"},
                {"@type": "Product", "name": "Broken"},
                {"@type": "Product", "name": "After"},
            ]
        ),
        URL,
    )

    names = [r.data.get("name") for r in records]
    assert names == ["Before", "After"], "a malformed item took its siblings with it"


def test_d_a_broken_block_does_not_discard_a_later_block():
    """Control: invalid JSON in one script tag still costs only that tag."""
    html = (
        '<html><body>'
        '<script type="application/ld+json">{not json at all</script>'
        '<script type="application/ld+json">'
        '{"@type": "Product", "name": "Survivor"}'
        "</script></body></html>"
    )
    records = pipeline()._extract_json_ld(Selector(html), URL)

    assert [r.data.get("name") for r in records] == ["Survivor"]


def test_e_ordinary_items_are_unchanged():
    """Control: the common string-valued @type still lands as before."""
    records = pipeline()._extract_json_ld(
        page({"@type": "Product", "name": "Plain"}), URL
    )

    assert len(records) == 1
    assert records[0].record_type == "product"
    assert records[0].source_url == URL


def test_f_a_graph_block_still_expands():
    """Control: @graph expansion must survive the narrowed except scope."""
    records = pipeline()._extract_json_ld(
        page(
            {
                "@graph": [
                    {"@type": ["Organization", "Thing"], "name": "Org"},
                    {"@type": "Person", "name": "Someone"},
                ]
            }
        ),
        URL,
    )

    assert [r.data.get("name") for r in records] == ["Org", "Someone"]
