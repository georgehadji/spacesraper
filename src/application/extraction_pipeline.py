# Merged extraction pipeline — v2.
# Wires the strategy dispatch chain into the deterministic extraction pipeline.
#
# Strategy dispatch priority:
#   1. page_fields (user-specified OverrideStrategy)     — highest
#   2. Google Maps Place page (GoogleMapsPlaceStrategy)
#   3. Google Maps Search (GoogleMapsStrategy)            — domain-specific
#   4. ExtractionOverlay (declarative field mappings)     — declarative
#   5. JSON-LD structured data                            — generic fallbacks
#   6. Semantic HTML patterns (articles, lists, tables)
#
# A failed optional stage does not erase results from earlier stages.

import hashlib
import json
import logging
import uuid

from bs4 import BeautifulSoup

from src.application.deduplicator import Deduplicator
from src.domain.models import ExtractedRecord, ExtractionOverlay, ExtractionSchema, ProcessingResult, RawScrapePayload
from src.domain.ports import OverlayRepository
from src.extractors.base_extractor import BaseExtractionStrategy
from src.extractors.strategies import GenericStrategy, GoogleMapsPlaceStrategy, GoogleMapsStrategy, OverrideStrategy

logger = logging.getLogger("Spacescraper.ExtractionPipeline")

# P7.3: the old list rule fired on any <ul>/<ol> with >= 3 <li> — which is
# every navigation menu, footer link block, and breadcrumb on the web.
_LIST_NOISE_ANCESTOR_TAGS = ("nav", "footer", "header", "aside")


def _is_bare_link_item(li) -> bool:
    """An <li> that is just a link wrapping its own text — the shape of a
    nav-menu/breadcrumb entry, not real list content."""
    links = li.find_all("a")
    if len(links) != 1:
        return False
    return li.get_text(strip=True) == links[0].get_text(strip=True)


class DeterministicExtractionPipeline(BaseExtractionStrategy):
    """
    Merged strategy chain for extraction.

    Strategy dispatch:
      - page_fields on the job submission? → OverrideStrategy (user mappings)
      - URL matches google.com/maps/place/? → GoogleMapsPlaceStrategy
      - URL matches google.com/maps/search? → GoogleMapsStrategy
      - ExtractionOverlay exists for domain? → overlay extraction
      - Fall through → JSON-LD → Semantic HTML
    """

    def __init__(self, overlay_repo: OverlayRepository | None = None):
        self.overlay_repo = overlay_repo
        self._generic = GenericStrategy()
        self._override = OverrideStrategy()
        self._gm = GoogleMapsStrategy()
        self._gm_place = GoogleMapsPlaceStrategy()

    # ------------------------------------------------------------------
    # Main dispatch entry point
    # ------------------------------------------------------------------

    async def extract(
        self,
        html: str,
        json_payloads: list[dict],
        current_url: str = "",
        overlay: dict | None = None,
        schema: ExtractionSchema | None = None,
    ) -> list[ExtractedRecord]:
        """Run the full strategy chain. Returns validated records."""
        soup = BeautifulSoup(html, "html.parser")
        all_records: list[ExtractedRecord] = []

        # -----------------------------------------------------------------
        # Stage A: page_fields override — user-specified selectors win
        # -----------------------------------------------------------------
        if isinstance(overlay, dict) and overlay.get("mappings"):
            records = await self._override.extract(html, json_payloads, current_url, overlay, schema)
            if records:
                validated = self._validate_records(records, schema)
                logger.info("Pipeline(dispatch): Override produced %d records", len(validated))
                return validated

        # -----------------------------------------------------------------
        # Stage B: Google Maps Place detail page
        # -----------------------------------------------------------------
        if self._gm_place.matches_domain(current_url):
            records = await self._gm_place.extract(html, json_payloads, current_url, overlay, schema)
            if records:
                validated = self._validate_records(records, schema)
                logger.info("Pipeline(dispatch): GoogleMapsPlace produced %d records", len(validated))
                return validated

        # -----------------------------------------------------------------
        # Stage C: Google Maps Search results
        # -----------------------------------------------------------------
        if self._gm.matches_domain(current_url):
            records = await self._gm.extract(html, json_payloads, current_url, overlay, schema)
            if records:
                validated = self._validate_records(records, schema)
                logger.info("Pipeline(dispatch): GoogleMapsSearch produced %d records", len(validated))
                return validated

        # -----------------------------------------------------------------
        # Stage D: Overlay (declarative, domain-specific)
        # -----------------------------------------------------------------
        overlay_records = await self._try_overlay(soup, current_url, overlay)
        if overlay_records:
            validated = self._validate_records(overlay_records, schema)
            logger.info("Pipeline(dispatch): Overlay produced %d valid records", len(validated))
            return validated  # overlay is authoritative when present

        # -----------------------------------------------------------------
        # Stage E: JSON-LD
        # -----------------------------------------------------------------
        json_ld_records = self._extract_json_ld(soup, current_url)
        if json_ld_records:
            validated = self._validate_records(json_ld_records, schema)
            all_records.extend(validated)

        # -----------------------------------------------------------------
        # Stage F: Semantic HTML (articles, lists, tables)
        # -----------------------------------------------------------------
        if not json_ld_records:
            html_records = self._extract_semantic_html(soup, current_url)
            if html_records:
                validated = self._validate_records(html_records, schema)
                all_records.extend(validated)

        if not all_records:
            logger.debug("Pipeline(dispatch): No records extracted from %s", current_url)

        return all_records

    # ------------------------------------------------------------------
    # Overlay (declarative extraction)
    # ------------------------------------------------------------------

    async def _try_overlay(
        self, soup: BeautifulSoup, current_url: str, overlay: dict | None
    ) -> list[ExtractedRecord]:
        """Try running an overlay, either explicit or from the repository."""
        if overlay:
            return self._apply_overlay_dict(soup, current_url, overlay)
        if self.overlay_repo:
            from urllib.parse import urlparse
            domain = urlparse(current_url).netloc
            active = await self.overlay_repo.get_active_overlay(domain)
            if active and active.field_mappings:
                return self._apply_field_mappings(soup, current_url, active)
        return []

    def _apply_overlay_dict(
        self, soup: BeautifulSoup, current_url: str, overlay: dict
    ) -> list[ExtractedRecord]:
        """Apply an inline overlay dictionary directly."""
        container_selector = overlay.get("container_selector")
        field_mappings = overlay.get("field_mappings", {})
        if not field_mappings:
            return []
        containers = soup.select(container_selector) if container_selector else [soup]
        records: list[ExtractedRecord] = []
        for el in containers:
            data: dict[str, object] = {}
            for field, selector in field_mappings.items():
                found = el.select_one(selector)
                if found:
                    data[field] = found.get_text(strip=True)
            if data:
                record = ExtractedRecord(
                    record_id=f"rec_{uuid.uuid4().hex[:12]}",
                    record_type="generic",
                    data=data,
                    source_url=current_url,
                )
                record.compute_identity_hash()
                records.append(record)
        return records

    def _apply_field_mappings(
        self, soup: BeautifulSoup, current_url: str, overlay: ExtractionOverlay
    ) -> list[ExtractedRecord]:
        """Apply an ExtractionOverlay from the repository."""
        cs = overlay.container_selector
        containers = soup.select(cs) if cs else [soup]
        records: list[ExtractedRecord] = []
        for el in containers:
            data: dict[str, object] = {}
            for field, selector in overlay.field_mappings.items():
                found = el.select_one(selector)
                if found:
                    data[field] = found.get_text(strip=True)
            if data:
                record = ExtractedRecord(
                    record_id=f"rec_{uuid.uuid4().hex[:12]}",
                    record_type="generic",
                    data=data,
                    source_url=current_url,
                )
                record.compute_identity_hash()
                records.append(record)
        return records

    # ------------------------------------------------------------------
    # JSON-LD extraction
    # ------------------------------------------------------------------

    def _extract_json_ld(self, soup: BeautifulSoup, current_url: str) -> list[ExtractedRecord]:
        """Parse JSON-LD script tags into ExtractedRecords."""
        records: list[ExtractedRecord] = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict):
                    data = [data]
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    # Expand @graph into individual records
                    graph = item.get("@graph")
                    if isinstance(graph, list):
                        for graph_item in graph:
                            if isinstance(graph_item, dict):
                                records.append(self._make_json_ld_record(graph_item, current_url))
                    else:
                        records.append(self._make_json_ld_record(item, current_url))
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
        return records

    def _make_json_ld_record(self, item: dict, current_url: str) -> ExtractedRecord:
        """Create an ExtractedRecord from a JSON-LD item."""
        record = ExtractedRecord(
            record_id=f"rec_{uuid.uuid4().hex[:12]}",
            record_type=item.get("@type", "structured_data").lower(),
            data=item,
            source_url=current_url,
        )
        record.compute_identity_hash()
        return record

    # ------------------------------------------------------------------
    # Semantic HTML extraction
    # ------------------------------------------------------------------

    def _extract_semantic_html(self, soup: BeautifulSoup, current_url: str) -> list[ExtractedRecord]:
        """Extract generic semantic HTML patterns (articles, tables, lists)."""
        records: list[ExtractedRecord] = []

        # Articles
        for article in soup.find_all("article"):
            title = article.find(["h1", "h2", "h3"])
            text = article.get_text(strip=True)
            if text and len(text) > 50:
                record = ExtractedRecord(
                    record_id=f"rec_{uuid.uuid4().hex[:12]}",
                    record_type="article",
                    data={
                        "title": title.get_text(strip=True) if title else "",
                        "text": text[:5000],
                    },
                    source_url=current_url,
                )
                record.compute_identity_hash()
                records.append(record)

        # Tables
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True) for th in table.find_all("th")]
            rows: list[list[str]] = []
            for tr in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if cells:
                    rows.append(cells)
            if headers or rows:
                record = ExtractedRecord(
                    record_id=f"rec_{uuid.uuid4().hex[:12]}",
                    record_type="table",
                    data={"headers": headers, "rows": rows},
                    source_url=current_url,
                )
                record.compute_identity_hash()
                records.append(record)

        # Lists (ul > li patterns) — scoped per P7.3: skip nav/footer/header/
        # aside ancestors, and skip lists whose items are all bare links
        # (the shape of a nav menu even without a semantic <nav> wrapper).
        for lst in soup.find_all(["ul", "ol"]):
            if lst.find_parent(_LIST_NOISE_ANCESTOR_TAGS):
                continue
            li_tags = lst.find_all("li")
            if len(li_tags) < 3:
                continue
            if all(_is_bare_link_item(li) for li in li_tags):
                continue
            items = [li.get_text(strip=True) for li in li_tags]
            record = ExtractedRecord(
                record_id=f"rec_{uuid.uuid4().hex[:12]}",
                record_type="list",
                data={"items": items},
                source_url=current_url,
            )
            record.compute_identity_hash()
            records.append(record)

        return records

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_records(
        self, records: list[ExtractedRecord], schema: ExtractionSchema | None
    ) -> list[ExtractedRecord]:
        """Filter records through schema validation if a schema is provided."""
        if not schema:
            return records
        valid: list[ExtractedRecord] = []
        for r in records:
            errors = schema.validate_record(r.data)
            if errors:
                logger.debug(
                    "Pipeline: record failed schema validation: %s", errors
                )
            else:
                valid.append(r)
        return valid


class ExtractionPipeline:
    """
    Spacescraper Intelligence Orchestrator (W2.2).

    Replaces pipeline.py::DataPipeline. Same `.process()` contract (RawScrapePayload,
    strategy) -> ProcessingResult, so callers need no interface changes — only the
    strategy and orchestrator implementations swap. Delegates field extraction to a
    BaseExtractionStrategy (DeterministicExtractionPipeline on the live path) and
    near-duplicate removal to Deduplicator, keeping this class itself thin.

    AI enrichment (C7) is not reintroduced: DataPipeline's ai_enrichment_enabled
    flag gated a stub that returned immediately on every call, so removing it
    changes no live behavior. Reintroducing enrichment for real is unrelated
    feature work, tracked separately if it's ever wanted.
    """

    def __init__(self, deduplicator: Deduplicator | None = None):
        self.deduplicator = deduplicator or Deduplicator()

    async def process(self, payload: RawScrapePayload, strategy: BaseExtractionStrategy) -> ProcessingResult:
        result = ProcessingResult(job_id=payload.job_id, success=False)

        if payload.status_code >= 400 or payload.error_message:
            result.error = payload.error_message
            return result

        try:
            logger.info("Spacescraper: Dispatching to %s", strategy.__class__.__name__)
            records = await strategy.extract(
                payload.html_content,
                payload.json_payloads,
                current_url=payload.url,
                overlay=payload.overlay,
            )

            for record in records:
                self._ensure_hashes(record)

            unique_records = self.deduplicator.dedupe(records)

            result.success = True
            result.entities = unique_records
            # Discovery (FollowLink) is not wired to any live strategy today — no
            # strategy on the live path constructs FollowLink instances, so this
            # was already always empty under DataPipeline too. Not a W2.2 regression.
            result.follow_urls = []

        except Exception as e:
            logger.exception("Spacescraper Pipeline Critical Error: %s", e)
            result.error = f"Pipeline Internal Error: {e}"

        return result

    @staticmethod
    def _ensure_hashes(record: ExtractedRecord) -> None:
        """Guarantee both hashes are set regardless of which strategy branch built the record."""
        if not record.identity_hash:
            record.compute_identity_hash()
        if not record.content_hash:
            record.content_hash = hashlib.sha256(
                json.dumps(record.data, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
