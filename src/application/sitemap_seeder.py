# P2: sitemap discovery. docs/plans/2026-08-13-capability-enhancement-plan.md P2.

import logging
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

from src.infrastructure.http_client import target_http

logger = logging.getLogger("Spacescraper.SitemapSeeder")

MAX_SITEMAP_URLS = 500  # bounded — a huge sitemap must not be read unbounded into memory
_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


async def _find_sitemap_locations(root_url: str) -> list[str]:
    """robots.txt Sitemap: lines first, falling back to the /sitemap.xml
    well-known path — both are best-effort; a fetch failure just means
    fewer candidates, not an error."""
    origin = f"{urlparse(root_url).scheme}://{urlparse(root_url).netloc}"
    locations: list[str] = []
    try:
        response = await target_http.get(f"{origin}/robots.txt")
        if response.status_code == 200:
            for line in response.text.splitlines():
                if line.strip().lower().startswith("sitemap:"):
                    locations.append(line.split(":", 1)[1].strip())
    except Exception:
        logger.debug("robots.txt fetch failed while seeding sitemaps for %s", origin, exc_info=True)

    if not locations:
        locations.append(urljoin(origin, "/sitemap.xml"))
    return locations


def _parse_sitemap_xml(xml_text: str) -> tuple[list[str], list[str]]:
    """Returns (page_urls, nested_sitemap_urls) from one sitemap document —
    caller decides whether to recurse into the nested ones."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [], []

    tag = root.tag.rsplit("}", 1)[-1]
    locs = [
        loc.text.strip()
        for entry in root
        for loc in entry.findall(f"{_SITEMAP_NS}loc")
        if loc.text
    ]
    if tag == "sitemapindex":
        return [], locs
    return locs, []


async def discover_sitemap_urls(root_url: str, *, max_urls: int = MAX_SITEMAP_URLS) -> list[str]:
    """Discovers and flattens sitemap URLs for a site. Recurses one level
    into a sitemap index (index-of-indexes is real but rare; not handled
    here — upgrade path if a target ever needs it)."""
    discovered: list[str] = []
    for location in await _find_sitemap_locations(root_url):
        try:
            response = await target_http.get(location)
        except Exception:
            logger.debug("Sitemap fetch failed for %s", location, exc_info=True)
            continue
        if response.status_code != 200:
            continue

        page_urls, nested = _parse_sitemap_xml(response.text)
        discovered.extend(page_urls)

        for nested_url in nested:
            if len(discovered) >= max_urls:
                break
            try:
                nested_response = await target_http.get(nested_url)
            except Exception:
                logger.debug("Nested sitemap fetch failed for %s", nested_url, exc_info=True)
                continue
            if nested_response.status_code == 200:
                nested_page_urls, _ = _parse_sitemap_xml(nested_response.text)
                discovered.extend(nested_page_urls)

        if len(discovered) >= max_urls:
            break

    return discovered[:max_urls]
