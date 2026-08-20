# S5: prompt injection into executable configuration.
#
# Gate 1 (sanitize_for_llm) strips the parts of scraped HTML that exist only
# to carry instructions to a model reading the DOM rather than a human
# looking at the rendered page: CSS-hidden / aria-hidden / <template>
# subtrees, comments, and zero-width / C0 control characters hidden inside
# otherwise-visible text. It runs ahead of compact_html_for_prompt, which
# shrinks tokens but is not a safety pass.
#
# Gate 2 (validate_overlay) treats an LLM-returned overlay as untrusted
# configuration: every selector must parse, resolve against the sampled
# HTML, stay within the requested field schema, and stay inside the
# extraction container. It does not decide CANDIDATE vs ACTIVE — that stays
# behind the existing shadow-evaluator/human promotion gate; this only
# blocks persisting something that cannot even be worth evaluating.

import re
from collections.abc import Iterable
from typing import Any

from bs4 import BeautifulSoup, Comment, Tag
from bs4.element import NavigableString

# U+200B-200D (zero-width space/non-joiner/joiner), U+FEFF (BOM), U+2060
# (word joiner), U+180E (Mongolian vowel separator). Built from codepoints,
# not typed as literals — these characters are themselves invisible, so a
# literal in source would be unreviewable in a diff.
_ZERO_WIDTH_CODEPOINTS = (0x200B, 0x200C, 0x200D, 0xFEFF, 0x2060, 0x180E)
_ZERO_WIDTH_RE = re.compile("[" + "".join(chr(c) for c in _ZERO_WIDTH_CODEPOINTS) + "]")
_C0_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def strip_hidden_chars(text: str) -> str:
    """Remove zero-width and C0 control characters from `text`.

    Shared primitive: sanitize_for_llm uses it on parsed HTML text nodes;
    callers sending already-extracted (non-HTML) text to a model — e.g.
    enrichment prompts built from structured field values — can call it
    directly.
    """
    return _C0_CONTROL_RE.sub("", _ZERO_WIDTH_RE.sub("", text))


def _is_hidden(tag: Tag) -> bool:
    if tag.name == "template":
        return True
    if str(tag.get("aria-hidden", "")).strip().lower() == "true":
        return True
    if tag.has_attr("hidden"):
        return True
    # ponytail: substring match on a lowercased, space-stripped style
    # attribute — misses "display : none" spacing variants and inherited
    # hidden-via-CSS-class. Upgrade to a real CSS parser if that's exploited.
    style = str(tag.get("style", "")).lower().replace(" ", "")
    return "display:none" in style or "visibility:hidden" in style


def sanitize_for_llm(html: str) -> str:
    """
    Strip prompt-injection surface from `html` before it is spent as model
    input: CSS-hidden / aria-hidden / <template> subtrees, comments, and
    zero-width/C0 control characters in visible text. Safe on untrusted
    scraped markup — falls back to char-stripping only if parsing fails.
    """
    if not isinstance(html, str) or not html:
        return ""

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return strip_hidden_chars(html)

    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    for tag in soup.find_all(_is_hidden):
        if tag.parent is not None:
            tag.decompose()

    for node in soup.find_all(string=True):
        node.replace_with(NavigableString(strip_hidden_chars(str(node))))

    return str(soup)


def validate_overlay(
    overlay: dict[str, Any], sample_html: str, schema: Iterable[str]
) -> list[str]:
    """
    Validate an LLM-returned extraction overlay against the sampled HTML it
    was generated from. Returns a list of human-readable defects; empty
    means the overlay may be persisted (as CANDIDATE — promotion to ACTIVE
    is a separate, existing gate).
    """
    errors: list[str] = []

    container_selector = overlay.get("container_selector")
    field_mappings = overlay.get("field_mappings")

    if not isinstance(container_selector, str) or not container_selector.strip():
        errors.append("container_selector is missing or not a string")
        return errors

    if not isinstance(field_mappings, dict):
        errors.append("field_mappings is missing or not a dict")
        return errors

    schema_set = set(schema)
    mapped_fields = set(field_mappings.keys())
    missing = schema_set - mapped_fields
    extra = mapped_fields - schema_set
    if missing:
        errors.append(f"field_mappings missing required fields: {sorted(missing)}")
    if extra:
        errors.append(f"field_mappings has fields outside the schema: {sorted(extra)}")

    soup = BeautifulSoup(sample_html, "html.parser")

    try:
        containers = soup.select(container_selector)
    except Exception as e:
        errors.append(f"container_selector does not parse: {e}")
        return errors

    if not containers:
        errors.append("container_selector does not resolve against the sampled HTML")
        return errors

    container_scope: set[int] = set()
    for container in containers:
        container_scope.add(id(container))
        for descendant in container.find_all(True):
            container_scope.add(id(descendant))

    for field, selector in field_mappings.items():
        if not isinstance(selector, str) or not selector.strip():
            errors.append(f"field '{field}': selector is missing or not a string")
            continue
        try:
            matches = soup.select(selector)
        except Exception as e:
            errors.append(f"field '{field}': selector does not parse ({e})")
            continue
        if not matches:
            errors.append(f"field '{field}': selector does not resolve against the sampled HTML")
            continue
        if not all(id(m) in container_scope for m in matches):
            errors.append(f"field '{field}': selector resolves outside container_selector")

    return errors
