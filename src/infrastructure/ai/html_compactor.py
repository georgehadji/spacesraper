# Shrinks raw HTML before it is spent as LLM prompt tokens.
#
# Raw markup is extremely token-dense and mostly signal-free for selector
# inference: scripts, inline styles, tracking attributes and base64 data URIs
# dominate the byte count while contributing nothing to "which selector holds
# the title". Stripping them typically removes the large majority of the input
# while preserving the structure a model needs.

import logging
import re
from typing import Iterable

from bs4 import BeautifulSoup, Comment

logger = logging.getLogger("Spacescraper.HtmlCompactor")

# Tags whose content is never useful for structural inference.
_DROP_TAGS = (
    "script", "style", "noscript", "svg", "canvas", "iframe",
    "picture", "source", "track", "template",
)

# Attributes worth keeping: these are what a CSS selector can actually target.
_KEEP_ATTRS = frozenset({
    "class", "id", "href", "name", "role", "type",
    "itemprop", "itemtype", "property", "rel",
})

_WHITESPACE_RE = re.compile(r"\s+")


def _clean_attrs(tag) -> None:
    """Drop attributes a selector would never use (style, inline JS, data URIs)."""
    for attr in list(tag.attrs):
        if attr not in _KEEP_ATTRS:
            del tag[attr]
            continue
        value = tag.get(attr)
        # Long href values are usually tracking URLs or base64 payloads.
        if isinstance(value, str) and len(value) > 300:
            tag[attr] = value[:300]


def compact_html_for_prompt(
    html: str,
    max_chars: int = 6000,
    drop_tags: Iterable[str] = _DROP_TAGS,
) -> str:
    """
    Reduce `html` to the smallest form that still supports selector inference.

    Removes non-structural tags, HTML comments, and selector-irrelevant
    attributes, then collapses whitespace and truncates to `max_chars`.
    Falls back to a plain truncation if parsing fails, so this is always safe
    to call on untrusted scraped markup.
    """
    if not isinstance(html, str) or not html:
        return ""

    try:
        soup = BeautifulSoup(html, "html.parser")

        for tag_name in drop_tags:
            for tag in soup.find_all(tag_name):
                tag.decompose()

        for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()

        for tag in soup.find_all(True):
            _clean_attrs(tag)

        compacted = _WHITESPACE_RE.sub(" ", str(soup)).strip()
    except Exception as e:
        logger.debug("HtmlCompactor: parse failed (%s), falling back to raw slice.", e)
        return html[:max_chars]

    # Never return more than the raw slice would have cost.
    if len(compacted) > len(html):
        compacted = html

    return compacted[:max_chars]
