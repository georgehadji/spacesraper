# Regression tests for P8.2: export primitives. The deliverable test is a
# record with control characters and a nested dict round-tripping through
# all four formats without crashing or silently losing data.

import csv
import io
import json
import xml.etree.ElementTree as ET

import pytest

from src.domain.models import ExtractedRecord
from src.infrastructure.exports.record_exporters import to_csv, to_json, to_jsonl, to_xml


@pytest.fixture
def messy_record() -> ExtractedRecord:
    return ExtractedRecord(
        record_id="rec_messy",
        record_type="opportunity",
        source_url="https://example.com/x",
        data={
            "title": "Widget\x01\x02 deal",
            "meta": {"buyer": "Acme", "tags": ["a", "b"]},
            "2bad key!": "v",
        },
    )


def test_to_json_round_trips(messy_record):
    parsed = json.loads(to_json([messy_record]))
    assert parsed[0]["record_id"] == "rec_messy"
    assert parsed[0]["data"]["meta"]["buyer"] == "Acme"
    assert parsed[0]["data"]["meta"]["tags"] == ["a", "b"]


def test_to_jsonl_round_trips(messy_record):
    lines = to_jsonl([messy_record]).splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["data"]["meta"]["buyer"] == "Acme"


def test_to_csv_nested_dict_serializes_as_json_not_str_dict(messy_record):
    rows = list(csv.DictReader(io.StringIO(to_csv([messy_record]))))
    assert len(rows) == 1
    assert rows[0]["record_id"] == "rec_messy"
    # Not Python's str(dict) shape (single quotes) — real JSON.
    meta = json.loads(rows[0]["meta"])
    assert meta["buyer"] == "Acme"


def test_to_csv_unions_key_set_across_heterogeneous_records():
    a = ExtractedRecord(record_id="a", record_type="t", source_url="https://x/a", data={"title": "A"})
    b = ExtractedRecord(record_id="b", record_type="t", source_url="https://x/b", data={"buyer": "B"})
    rows = list(csv.DictReader(io.StringIO(to_csv([a, b]))))
    assert "title" in rows[0] and "buyer" in rows[0]
    assert rows[0]["buyer"] == ""
    assert rows[1]["title"] == ""


def test_to_xml_strips_control_chars_and_is_parseable(messy_record):
    xml_text = to_xml([messy_record])
    root = ET.fromstring(xml_text)  # must not raise
    title = root.find("./record/data/title")
    assert title is not None
    assert "\x01" not in title.text
    assert "\x02" not in title.text
    assert "Widget" in title.text and "deal" in title.text


def test_to_xml_rewrites_invalid_tag_name_and_preserves_original(messy_record):
    xml_text = to_xml([messy_record])
    root = ET.fromstring(xml_text)
    data_el = root.find("./record/data")
    rewritten = [el for el in data_el if el.tag not in ("title", "meta")]
    assert len(rewritten) == 1
    assert rewritten[0].get("name") == "2bad key!"
    assert rewritten[0].text == "v"


def test_all_four_formats_handle_empty_record_list():
    assert json.loads(to_json([])) == []
    assert to_jsonl([]) == ""
    assert to_csv([]).strip() != ""  # header row still emitted
    assert ET.fromstring(to_xml([])).tag == "records"


def test_to_csv_data_key_colliding_with_base_field_cannot_spoof_it():
    # `data` is untrusted (LLM/extractor output from a scraped page); a
    # colliding key must not let scraped content overwrite the record's own
    # identity metadata or create a duplicate CSV column.
    r = ExtractedRecord(
        record_id="real-123",
        record_type="listing",
        source_url="https://evil.example/page",
        data={"record_id": "SPOOFED", "title": "Widget"},
    )
    text = to_csv([r])
    assert text.splitlines()[0].split(",").count("record_id") == 1
    rows = list(csv.DictReader(io.StringIO(text)))
    assert rows[0]["record_id"] == "real-123"
    assert rows[0]["title"] == "Widget"


def test_to_csv_data_key_colliding_with_every_base_field():
    r = ExtractedRecord(
        record_id="r1",
        record_type="t1",
        source_url="https://x/1",
        canonical_url="https://x/canon",
        data={
            "record_id": "x",
            "record_type": "x",
            "source_url": "x",
            "canonical_url": "x",
            "extracted_at": "x",
        },
    )
    rows = list(csv.DictReader(io.StringIO(to_csv([r]))))
    assert rows[0]["record_id"] == "r1"
    assert rows[0]["record_type"] == "t1"
    assert rows[0]["source_url"] == "https://x/1"
    assert rows[0]["canonical_url"] == "https://x/canon"
