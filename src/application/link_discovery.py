# P2: link discovery — pure functions over an already-fetched page's HTML.
# docs/plans/2026-08-13-capability-enhancement-plan.md P2.

import fnmatch
from urllib.parse import urljoin, urlparse

from scrapling import Selector

MAX_LINKS_PER_PAGE = 50


def extract_links(
    html: str, base_url: str, *,
    include_globs: list[str] | None = None,
    exclude_globs: list[str] | None = None,
) -> list[str]:
    """<a href> extraction with relative-URL resolution. Scrapy's
    allowed_domains idiom: same-registrable-domain-only unless
    include_globs explicitly widens scope. exclude_globs always applies.
    Capped at MAX_LINKS_PER_PAGE — the fan-out budget caps the crawl overall,
    but one page shouldn't be able to claim the whole budget by itself."""
    if not html:
        return []

    base_domain = urlparse(base_url).netloc
    seen: set[str] = set()
    urls: list[str] = []

    for anchor in Selector(html).css("a[href]"):
        href = anchor.attrib.get("href")
        if not href:
            continue
        absolute = urljoin(base_url, href.split("#", 1)[0])
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https") or absolute in seen:
            continue
        if include_globs:
            if not any(fnmatch.fnmatch(absolute, g) for g in include_globs):
                continue
        elif parsed.netloc != base_domain:
            continue
        if exclude_globs and any(fnmatch.fnmatch(absolute, g) for g in exclude_globs):
            continue

        seen.add(absolute)
        urls.append(absolute)
        if len(urls) >= MAX_LINKS_PER_PAGE:
            break

    return urls


_NEXT_PAGE_SELECTORS = ('a[rel="next"]', 'link[rel="next"]', 'a.next', 'a.pagination-next')


def find_next_page_url(html: str, base_url: str) -> str | None:
    """rel="next" detection only — URL-pattern (?page=N) pagination and
    bounded infinite-scroll are real scope, not built this pass; the browser
    engine has no scroll-and-settle loop yet (would need work in
    src/infrastructure/browser/engine.py's crawl()), and heuristic
    ?page=N-style URL guessing without a rel=next anchor risks false
    positives on plain query-string content. Upgrade path: add both when a
    domain needing them shows up."""
    if not html:
        return None
    selector = Selector(html)
    for css in _NEXT_PAGE_SELECTORS:
        match = selector.css(css).first
        if match is None:
            continue
        href = match.attrib.get("href")
        if href:
            return urljoin(base_url, href.split("#", 1)[0])
    return None
