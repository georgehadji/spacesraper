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
from typing import List, Optional, Tuple, Dict
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.domain.models import ExtractedRecord, ExtractionSchema, ExtractionOverlay
from src.domain.ports import OverlayRepository
from src.extractors.base_extractor import BaseExtractionStrategy
from src.extractors.strategies import GenericStrategy, GoogleMapsStrategy, GoogleMapsPlaceStrategy, OverrideStrategy
from src.domain.exceptions import ExtractionError

logger = logging.getLogger("Spacescraper.ExtractionPipeline")


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

    def __init__(self, overlay_repo: Optional[OverlayRepository] = None):
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
        json_payloads: List[dict],
        current_url: str = "",
        overlay: Optional[dict] = None,
        schema: Optional[ExtractionSchema] = None,
    ) -> List[ExtractedRecord]:
        """Run the full strategy chain. Returns validated records."""
        soup = BeautifulSoup(html, "html.parser")
        all_records: List[ExtractedRecord] = []

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
        self, soup: BeautifulSoup, current_url: str, overlay: Optional[dict]
    ) -> List[ExtractedRecord]:
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
    ) -> List[ExtractedRecord]:
        """Apply an inline overlay dictionary directly."""
        container_selector = overlay.get("container_selector")
        field_mappings = overlay.get("field_mappings", {})
        if not field_mappings:
            return []
        containers = soup.select(container_selector) if container_selector else [soup]
        records: List[ExtractedRecord] = []
        for el in containers:
            data: Dict[str, object] = {}
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
    ) -> List[ExtractedRecord]:
        """Apply an ExtractionOverlay from the repository."""
        cs = overlay.container_selector
        containers = soup.select(cs) if cs else [soup]
        records: List[ExtractedRecord] = []
        for el in containers:
            data: Dict[str, object] = {}
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

    def _extract_json_ld(self, soup: BeautifulSoup, current_url: str) -> List[ExtractedRecord]:
        """Parse JSON-LD script tags into ExtractedRecords."""
        records: List[ExtractedRecord] = []
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

    def _extract_semantic_html(self, soup: BeautifulSoup, current_url: str) -> List[ExtractedRecord]:
        """Extract generic semantic HTML patterns (articles, tables, lists)."""
        records: List[ExtractedRecord] = []

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
            rows: List[List[str]] = []
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

        # Lists (ul > li patterns)
        for lst in soup.find_all(["ul", "ol"]):
            items = [li.get_text(strip=True) for li in lst.find_all("li")]
            if len(items) >= 3:
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
        self, records: List[ExtractedRecord], schema: Optional[ExtractionSchema]
    ) -> List[ExtractedRecord]:
        """Filter records through schema validation if a schema is provided."""
        if not schema:
            return records
        valid: List[ExtractedRecord] = []
        for r in records:
            errors = schema.validate_record(r.data)
            if errors:
                logger.debug(
                    "Pipeline: record failed schema validation: %s", errors
                )
            else:
                valid.append(r)
        return valid
