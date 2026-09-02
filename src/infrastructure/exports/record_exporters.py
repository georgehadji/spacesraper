# P8.2 - export primitives for the agent surface (P8.1's MCP server, once
# built, hands these back directly rather than raw model dumps). Four pure
# functions: list[ExtractedRecord] -> str. The two things that break real
# exports get explicit handling:
#   - CSV: non-scalar cells serialize as JSON, not str(dict); the column set
#     is the union of every record's `data` keys, so a field present on only
#     some records isn't silently dropped from the header.
#   - XML: characters outside the legal XML 1.0 range are stripped, and a
#     `data` key that isn't a valid XML element name is rewritten, with the
#     original key preserved in a `name` attribute.

import csv
import io
import json
import re
from xml.sax.saxutils import escape

from src.domain.models import ExtractedRecord

_BASE_CSV_FIELDS = ("record_id", "record_type", "source_url", "canonical_url", "extracted_at")

_XML_VALID_TAG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")


def to_json(records: list[ExtractedRecord]) -> str:
    """One JSON array of the full record set."""
    return json.dumps([r.model_dump(mode="json") for r in records], indent=2, default=str)


def to_jsonl(records: list[ExtractedRecord]) -> str:
    """One JSON object per line - streamable, no wrapping array."""
    return "\n".join(json.dumps(r.model_dump(mode="json"), default=str) for r in records)


def to_csv(records: list[ExtractedRecord]) -> str:
    """CSV with base record columns plus the union of every record's `data`
    keys. Non-scalar `data` values serialize as JSON text, not str(dict)."""
    data_fields: list[str] = []
    seen: set[str] = set(_BASE_CSV_FIELDS)
    for r in records:
        for key in r.data.keys():
            if key not in seen:
                seen.add(key)
                data_fields.append(key)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(_BASE_CSV_FIELDS) + data_fields, extrasaction="ignore")
    writer.writeheader()
    for r in records:
        row: dict[str, object] = {
            "record_id": r.record_id,
            "record_type": r.record_type,
            "source_url": r.source_url,
            "canonical_url": r.canonical_url or "",
            "extracted_at": r.extracted_at.isoformat(),
        }
        for key in data_fields:
            value = r.data.get(key, "")
            row[key] = json.dumps(value, default=str) if isinstance(value, (dict, list)) else value
        writer.writerow(row)
    return buf.getvalue()


def _is_legal_xml_char(codepoint: int) -> bool:
    """
    XML 1.0 Char production, decided by integer codepoint rather than a
    literal character class in source: those codepoints (a surrogate
    boundary and a byte-order-mark-adjacent noncharacter) are themselves
    unrepresentable or invisible, so a literal would be unreviewable in a
    diff. Legal: #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] |
    [#x10000-#x10FFFF].
    """
    tab, lf, cr = 0x9, 0xA, 0xD
    if codepoint in (tab, lf, cr):
        return True
    if 0x20 <= codepoint <= 0xD7FF:
        return True
    if 0xE000 <= codepoint <= 0xFFFD:
        return True
    return 0x10000 <= codepoint <= 0x10FFFF


def _strip_illegal_xml_chars(value: object) -> str:
    return "".join(ch for ch in str(value) if _is_legal_xml_char(ord(ch)))


def _safe_xml_tag(key: str) -> tuple[str, str | None]:
    """Returns (tag_name, original_key_or_None) - original_key is set only
    when the key had to be rewritten to become a valid XML element name."""
    if _XML_VALID_TAG_RE.match(key):
        return key, None
    sanitized = re.sub(r"[^A-Za-z0-9_.\-]", "_", key) or "field"
    if not re.match(r"^[A-Za-z_]", sanitized):
        sanitized = f"_{sanitized}"
    return sanitized, key


def _value_to_xml(value: object) -> str:
    if isinstance(value, dict):
        parts = []
        for key, val in value.items():
            tag, original = _safe_xml_tag(str(key))
            if original is not None:
                safe_original = escape(_strip_illegal_xml_chars(original), {'"': "&quot;"})
                attr = f' name="{safe_original}"'
            else:
                attr = ""
            parts.append(f"<{tag}{attr}>{_value_to_xml(val)}</{tag}>")
        return "".join(parts)
    if isinstance(value, list):
        return "".join(f"<item>{_value_to_xml(v)}</item>" for v in value)
    return escape(_strip_illegal_xml_chars(value))


def to_xml(records: list[ExtractedRecord]) -> str:
    """XML with illegal characters stripped and invalid `data` element names
    rewritten (original preserved as a `name` attribute)."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<records>"]
    for r in records:
        parts.append("<record>")
        parts.append(f"<record_id>{escape(_strip_illegal_xml_chars(r.record_id))}</record_id>")
        parts.append(f"<record_type>{escape(_strip_illegal_xml_chars(r.record_type))}</record_type>")
        parts.append(f"<source_url>{escape(_strip_illegal_xml_chars(r.source_url))}</source_url>")
        parts.append(f"<data>{_value_to_xml(r.data)}</data>")
        parts.append("</record>")
    parts.append("</records>")
    return "".join(parts)
