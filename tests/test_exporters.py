# Tests for the P8.2 export primitives.

import csv
import io
import json
from xml.etree import ElementTree

from src.application.exporters import _sanitize_xml_tag, to_csv, to_json, to_jsonl, to_xml
from src.domain.models import ExtractedRecord


def _record(**data) -> ExtractedRecord:
    r = ExtractedRecord(
        record_id="rec_1",
        record_type="generic",
        source_url="https://example.com",
        data=data,
    )
    r.compute_identity_hash()
    return r


def test_to_json_round_trips_nested_dict():
    records = [_record(name="Widget", offer={"price": "9.99", "currency": "USD"})]
    parsed = json.loads(to_json(records))
    assert parsed[0]["data"]["offer"] == {"price": "9.99", "currency": "USD"}


def test_to_jsonl_one_object_per_line():
    records = [_record(name="A"), _record(name="B")]
    lines = to_jsonl(records).splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["data"]["name"] == "A"
    assert json.loads(lines[1])["data"]["name"] == "B"


def test_to_csv_unions_heterogeneous_keys():
    """A field present on only one record must still get a column, not be
    silently dropped, and every row must have that column (even if empty)."""
    r1 = _record(name="A")
    r2 = _record(name="B", price="9.99")
    rows = list(csv.DictReader(io.StringIO(to_csv([r1, r2]))))
    assert "record_id" in rows[0]
    assert rows[0]["data"] == json.dumps({"name": "A"})
    assert rows[1]["data"] == json.dumps({"name": "B", "price": "9.99"})


def test_to_csv_serializes_nested_dict_as_json_not_repr():
    """str(dict) produces Python repr (single-quoted keys) which isn't valid
    JSON and breaks round-tripping; the cell must be real JSON instead."""
    records = [_record(offer={"price": "9.99"})]
    rows = list(csv.DictReader(io.StringIO(to_csv(records))))
    assert json.loads(rows[0]["data"]) == {"offer": {"price": "9.99"}}


def test_to_xml_strips_control_characters():
    records = [_record(note="before\x00after\x0b")]
    xml_str = to_xml(records)
    root = ElementTree.fromstring(xml_str)  # raises if the control chars broke well-formedness
    data_text = root.find("./record/data").text
    assert "\x00" not in data_text
    assert "\x0b" not in data_text
    assert "before" in data_text and "after" in data_text


def test_to_xml_rewrites_illegal_tag_name_and_preserves_original():
    tag, original = _sanitize_xml_tag("1st place")
    assert original == "1st place"
    assert tag[0].isalpha() or tag[0] == "_"
    assert " " not in tag

    records = [_record(**{"1st place": "gold"})]
    xml_str = to_xml(records)
    root = ElementTree.fromstring(xml_str)  # well-formed despite the odd field name
    assert root.find("./record/data") is not None


def test_all_four_formats_handle_control_chars_and_nested_dict_without_raising():
    records = [_record(note="weird\x00chars\x0b", offer={"price": "9.99", "tags": ["a", "b"]})]
    assert to_json(records)
    assert to_jsonl(records)
    assert to_csv(records)
    assert to_xml(records)


def test_empty_record_list():
    assert to_json([]) == "[]"
    assert to_jsonl([]) == ""
    assert to_csv([]) == ""
    root = ElementTree.fromstring(to_xml([]))
    assert list(root) == []
