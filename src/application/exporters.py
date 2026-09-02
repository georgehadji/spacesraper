# Export primitives for ExtractedRecord lists (P8.2).
#
# Four formats, one record list in, one string out. The two edge cases that
# break real exports (per the plan): XML must survive control characters and
# field names that aren't legal tag names; CSV must not silently drop fields
# across heterogeneous records or dump nested data as Python repr.

import csv
import io
import json
import re
from xml.sax.saxutils import escape as _xml_escape

from src.domain.models import ExtractedRecord

# C0 controls (minus tab/LF/CR, which XML allows) and C1 controls, plus DEL —
# the characters that are simply illegal in XML 1.0 text content.
_XML_ILLEGAL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_XML_TAG_INVALID_CHARS = re.compile(r"[^A-Za-z0-9_.-]")
_XML_TAG_VALID_START = re.compile(r"^[A-Za-z_]")


def to_json(records: list[ExtractedRecord]) -> str:
    """Pretty-printed JSON array of the records."""
    return json.dumps([r.model_dump(mode="json") for r in records], indent=2, ensure_ascii=False)


def to_jsonl(records: list[ExtractedRecord]) -> str:
    """One JSON object per line."""
    return "\n".join(json.dumps(r.model_dump(mode="json"), ensure_ascii=False) for r in records)


def to_csv(records: list[ExtractedRecord]) -> str:
    """CSV with the union of every record's keys as the header, so a field
    present on only some records doesn't get silently dropped. Non-scalar
    cells (the `data` dict, in particular) are serialized as JSON rather than
    Python's `str(dict)`, which isn't valid JSON and quotes keys wrong."""
    if not records:
        return ""
    rows = [r.model_dump(mode="json") for r in records]
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {k: json.dumps(v, default=str) if isinstance(v, dict | list) else v for k, v in row.items()}
        )
    return buf.getvalue()


def _sanitize_xml_tag(name: str) -> tuple[str, str | None]:
    """Rewrite `name` into a legal XML tag name. Returns (tag, original) —
    original is None when no rewrite was needed."""
    safe = _XML_TAG_INVALID_CHARS.sub("_", name) or "_field"
    if not _XML_TAG_VALID_START.match(safe):
        safe = f"_{safe}"
    return (safe, None) if safe == name else (safe, name)


def _xml_text(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return _xml_escape(_XML_ILLEGAL_CHARS.sub("", text))


def to_xml(records: list[ExtractedRecord]) -> str:
    """<records><record>...</record></records>. Field names that aren't
    legal XML tag names are rewritten, with the original preserved in a
    `name` attribute; characters outside XML's legal range are stripped
    (there is no legal way to represent them in XML text content)."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<records>"]
    for record in records:
        lines.append("  <record>")
        for key, value in record.model_dump(mode="json").items():
            tag, original = _sanitize_xml_tag(str(key))
            attr = f' name="{_xml_escape(original, {chr(34): "&quot;"})}"' if original else ""
            lines.append(f"    <{tag}{attr}>{_xml_text(value)}</{tag}>")
        lines.append("  </record>")
    lines.append("</records>")
    return "\n".join(lines)
