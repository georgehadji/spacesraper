# Deterministic extraction pipeline.
# Chains strategies in priority order: overlay -> JSON-LD -> semantic HTML.
# Each stage validates results against ExtractionSchema.
# A failed optional stage does not erase results from earlier stages.

import hashlib
import json
import logging
from typing import List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.domain.models import ExtractedRecord, ExtractionSchema, ExtractionOverlay
from src.domain.ports import OverlayRepository
from src.extractors.base_extractor import BaseExtractionStrategy
from src.domain.exceptions import ExtractionError

logger = logging.getLogger("Spacescraper.ExtractionPipeline")


class ExtractionStage:
    """Represents one stage of the deterministic extraction pipeline."""
    overlay: bool = False
    json_ld: bool = False
    semantic_html: bool = False


class DeterministicExtractionPipeline(BaseExtractionStrategy):
    """
    Sequential strategy chain for extraction.
    Order:
      1. Overlay (if available and ACTIVE for domain)
      2. JSON-LD structured data
      3. Semantic HTML patterns (articles, lists, tables)

    Each stage produces ExtractedRecord objects.
    Results from earlier stages are preserved if later stages fail.
    """

    def __init__(self, overlay_repo: Optional[OverlayRepository] = None):
        self.overlay_repo = overlay_repo

    async def extract(
        self,
        html: str,
        json_payloads: List[dict],
        current_url: str = "",
        overlay: Optional[dict] = None,
        schema: Optional[ExtractionSchema] = None,
    ) -> List[ExtractedRecord]:
        """Run the deterministic extraction chain. Returns validated records."""
        soup = BeautifulSoup(html, "html.parser")
        all_records: List[ExtractedRecord] = []

        # Stage 1: Overlay (highest priority)
        overlay_records = await self._try_overlay(soup, current_url, overlay)
        if overlay_records:
            validated = self._validate_records(overlay_records, schema)
            logger.info("ExtractionPipeline: Overlay produced %d valid records", len(validated))
            return validated  # overlay is authoritative when present

        # Stage 2: JSON-LD
        json_ld_records = self._extract_json_ld(soup, current_url)
        if json_ld_records:
            validated = self._validate_records(json_ld_records, schema)
            all_records.extend(validated)

        # Stage 3: Semantic HTML (only if JSON-LD produced nothing)
        if not json_ld_records:
            html_records = self._extract_semantic_html(soup, current_url)
            if html_records:
                validated = self._validate_records(html_records, schema)
                all_records.extend(validated)

        if not all_records:
            logger.debug("ExtractionPipeline: No records extracted from %s", current_url)

        return all_records

    async def _try_overlay(
        self, soup: BeautifulSoup, current_url: str, overlay: Optional[dict]
    ) -> List[ExtractedRecord]:
        """Try running an overlay, either explicit or from repository."""
        if overlay:
            return self._apply_overlay_dict(soup, current_url, overlay)
        if self.overlay_repo:
            try:
                from urllib.parse import urlparse
                domain = urlparse(current_url).netloc
                active_overlay = await self.overlay_repo.get_active_overlay(domain)
                if active_overlay and active_overlay.field_mappings:
                    logger.info("ExtractionPipeline: Using ACTIVE overlay for %s (v%d)", domain, active_overlay.version)
                    return self._apply_overlay_obj(soup, current_url, active_overlay)
            except Exception as e:
                logger.debug("ExtractionPipeline: Overlay repo error: %s", e)
        return []

    def _apply_overlay_dict(self, soup: BeautifulSoup, current_url: str, overlay: dict) -> List[ExtractedRecord]:
        """Apply an overlay passed as a dict (e.g. from API request)."""
        records = []
        container_sel = overlay.get("container")
        mapping = overlay.get("mapping", {})
        record_type = overlay.get("entity_type", "generic")

        if not container_sel:
            return []

        containers = soup.select(container_sel)
        for cont in containers:
            record = self._extract_from_container(cont, current_url, mapping, record_type)
            if record:
                records.append(record)
        return records

    def _apply_overlay_obj(self, soup: BeautifulSoup, current_url: str, overlay: ExtractionOverlay) -> List[ExtractedRecord]:
        """Apply an ExtractionOverlay from the repository."""
        records = []
        container_sel = overlay.container_selector
        mapping = overlay.field_mappings
        record_type = overlay.schema_id

        if not container_sel:
            return []

        containers = soup.select(container_sel)
        for cont in containers:
            record = self._extract_from_container(cont, current_url, mapping, record_type)
            if record:
                records.append(record)
        return records

    def _extract_from_container(
        self, container, current_url: str, mapping: dict, record_type: str
    ) -> Optional[ExtractedRecord]:
        """Extract fields from a container element using a mapping."""
        try:
            data = {"source_url": current_url}
            for field, selector in mapping.items():
                elem = container.select_one(selector)
                if elem:
                    if selector.endswith("[href]"):
                        data[field] = urljoin(current_url, elem["href"])
                    elif selector.endswith("[src]"):
                        data[field] = urljoin(current_url, elem["src"])
                    else:
                        data[field] = elem.get_text(strip=True)

            title = str(data.get("name") or data.get("title") or "unknown")
            link = str(data.get("url", current_url))
            content_str = json.dumps(data, sort_keys=True, default=str)
            record_id = f"ov_{hashlib.md5((link + title).encode(), usedforsecurity=False).hexdigest()[:16]}"

            return ExtractedRecord(
                record_id=record_id,
                record_type=record_type,
                source_url=current_url,
                canonical_url=link,
                data=data,
                content_hash=hashlib.sha256(content_str.encode()).hexdigest(),
            )
        except Exception as e:
            logger.debug("Overlay container extraction error: %s", e)
            return None

    def _extract_json_ld(self, soup: BeautifulSoup, current_url: str) -> List[ExtractedRecord]:
        """Extract from JSON-LD script tags."""
        records = []
        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            if not script.string:
                continue
            try:
                data = json.loads(script.string)
                items = data.get("@graph", [data]) if isinstance(data, dict) else data
                if not isinstance(items, list):
                    items = [items]

                for item in items:
                    item_type = item.get("@type", "Thing")
                    name = item.get("name") or item.get("headline", "")
                    if name:
                        content_str = json.dumps(item, sort_keys=True, default=str)
                        record = ExtractedRecord(
                            record_id=f"ld_{hashlib.md5((current_url + name).encode(), usedforsecurity=False).hexdigest()[:16]}",
                            record_type=item_type.lower(),
                            source_url=current_url,
                            canonical_url=item.get("url", current_url),
                            data=item,
                            content_hash=hashlib.sha256(content_str.encode()).hexdigest(),
                        )
                        records.append(record)
            except (json.JSONDecodeError, AttributeError):
                pass
        return records

    def _extract_semantic_html(self, soup: BeautifulSoup, current_url: str) -> List[ExtractedRecord]:
        """Extract from semantic HTML structures (articles, list items)."""
        records = []
        import re

        # Try articles first
        articles = soup.find_all("article")
        for article in articles:
            title_tag = article.find(["h1", "h2", "h3", "h4", ".title"])
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            if len(title) < 5:
                continue

            link_tag = article.find("a", href=True)
            link = urljoin(current_url, link_tag["href"]) if link_tag else current_url
            content_div = article.find(["div", "p", ".content", ".description"])
            content = content_div.get_text(strip=True)[:500] if content_div else ""

            data = {"title": title, "content": content}
            content_str = json.dumps(data, sort_keys=True, default=str)
            record = ExtractedRecord(
                record_id=f"html_{hashlib.md5((link + title).encode(), usedforsecurity=False).hexdigest()[:16]}",
                record_type="article",
                source_url=current_url,
                canonical_url=link,
                data=data,
                content_hash=hashlib.sha256(content_str.encode()).hexdigest(),
            )
            records.append(record)

        # If no articles, try list items
        if not records:
            selectors = ["li", "tr"]
            for sel in selectors:
                items = soup.find_all(sel, class_=re.compile(r"(item|product|listing|row|entry)", re.I))
                for item in items:
                    title_tag = item.find(["h2", "h3", "h4", ".title", ".name", "a"])
                    if not title_tag or not title_tag.get_text(strip=True):
                        continue
                    title = title_tag.get_text(strip=True)
                    if len(title) < 5:
                        continue

                    link_tag = title_tag if title_tag.name == "a" and title_tag.get("href") else item.find("a", href=True)
                    link = urljoin(current_url, link_tag["href"]) if link_tag and link_tag.get("href") else current_url

                    data = {"title": title}
                    price_tag = item.find(class_=re.compile(r"price|amount|cost", re.I))
                    if price_tag:
                        data["price"] = price_tag.get_text(strip=True)

                    content_str = json.dumps(data, sort_keys=True, default=str)
                    record = ExtractedRecord(
                        record_id=f"html_{hashlib.md5((link + title).encode(), usedforsecurity=False).hexdigest()[:16]}",
                        record_type="listing",
                        source_url=current_url,
                        canonical_url=link,
                        data=data,
                        content_hash=hashlib.sha256(content_str.encode()).hexdigest(),
                    )
                    records.append(record)

        return records

    def _validate_records(
        self, records: List[ExtractedRecord], schema: Optional[ExtractionSchema]
    ) -> List[ExtractedRecord]:
        """Filter records through schema validation. Logs validation errors."""
        if not schema:
            return records  # no schema = accept all

        valid = []
        for record in records:
            errors = schema.validate_record(record.data)
            if errors:
                logger.debug("ExtractionPipeline: Schema validation failed for %s: %s", record.record_id, errors)
            else:
                valid.append(record)
        return valid
