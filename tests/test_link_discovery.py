# P2: link discovery pure functions.
# docs/plans/2026-08-13-capability-enhancement-plan.md P2.

from src.application.link_discovery import extract_links, find_next_page_url


def test_extract_links_default_scopes_to_same_domain():
    html = '''
    <a href="/products/1">P1</a>
    <a href="https://other.com/x">External</a>
    <a href="https://example.com/products/2">P2</a>
    '''
    urls = extract_links(html, "https://example.com/list")
    assert urls == ["https://example.com/products/1", "https://example.com/products/2"]


def test_extract_links_include_globs_widen_scope():
    html = '<a href="https://partner.example.org/deal/1">Deal</a><a href="https://random.com/x">X</a>'
    urls = extract_links(
        html, "https://example.com/list", include_globs=["https://partner.example.org/*"]
    )
    assert urls == ["https://partner.example.org/deal/1"]


def test_extract_links_exclude_globs_prune_matches():
    html = '<a href="/products/1">P1</a><a href="/logout">Logout</a>'
    urls = extract_links(html, "https://example.com/", exclude_globs=["*/logout"])
    assert urls == ["https://example.com/products/1"]


def test_extract_links_drops_fragments_and_dedupes():
    html = '<a href="/x#top">A</a><a href="/x#bottom">B</a>'
    urls = extract_links(html, "https://example.com/")
    assert urls == ["https://example.com/x"]


def test_extract_links_ignores_non_http_schemes():
    html = '<a href="mailto:a@example.com">Mail</a><a href="javascript:void(0)">JS</a>'
    urls = extract_links(html, "https://example.com/")
    assert urls == []


def test_extract_links_empty_html_returns_empty():
    assert extract_links("", "https://example.com/") == []


def test_find_next_page_url_detects_rel_next():
    html = '<a rel="next" href="/list?page=2">Next</a>'
    assert find_next_page_url(html, "https://example.com/list") == "https://example.com/list?page=2"


def test_find_next_page_url_returns_none_without_a_next_link():
    assert find_next_page_url("<p>no pagination here</p>", "https://example.com/") is None
