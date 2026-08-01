# Author: Spacescraper
# Project: Spacescraper (Generic Extractor)
# Role: Schema-driven extraction strategy for generic web content.

import logging
import json
import re
import hashlib
from typing import List, Dict, Any, Optional
from urllib.parse import urljoin
from bs4 import BeautifulSoup

from src.extractors.base_extractor import BaseExtractionStrategy
from src.domain.models import ExtractedRecord, FollowLink, BaseEntity

logger = logging.getLogger("Spacescraper.UniversalStrategy")

class UniversalExtractionStrategy(BaseExtractionStrategy):
    """
    Generic extraction strategy using schema-driven approaches:
    1. Declarative overlay (highest priority)
    2. JSON-LD structured data
    3. Semantic HTML patterns
    """

    async def extract(self, html: str, json_payloads: List[Dict[str, Any]], current_url: str = "", overlay: Optional[Dict[str, Any]] = None) -> List[BaseEntity]:
        soup = BeautifulSoup(html, "html.parser")
        entities: List[BaseEntity] = []

        # 0. Declarative Overlay: Explicit mapping from config (High priority)
        if overlay:
            overlay_results = self._extract_overlay(soup, current_url, overlay)
            if overlay_results:
                return overlay_results

        # 1. JSON-LD Structured Data
        json_ld_records = self._extract_json_ld(soup, current_url)
        entities.extend(json_ld_records)

        # 2. Semantic HTML patterns for common structures
        if not json_ld_records:
            html_records = self._extract_semantic_html(soup, current_url)
            entities.extend(html_records)

        return entities

    def _extract_json_ld(self, soup: BeautifulSoup, current_url: str) -> List[ExtractedRecord]:
        """Extract structured data from JSON-LD script tags."""
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
                        record = ExtractedRecord(
                            record_id=f"ld_{hashlib.md5((current_url + name).encode(), usedforsecurity=False).hexdigest()[:16]}",
                            record_type=item_type.lower(),
                            source_url=current_url,
                            canonical_url=item.get("url", current_url),
                            data=item,
                            content_hash=hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest(),
                        )
                        records.append(record)
            except (json.JSONDecodeError, AttributeError):
                logger.debug("JSON-LD extraction: no parseable data found", extra={"url": current_url})
        return records

    def _extract_semantic_html(self, soup: BeautifulSoup, current_url: str) -> List[ExtractedRecord]:
        """Extract records from semantic HTML patterns (lists, tables, articles)."""
        records = []

        # Article detection
        articles = soup.find_all("article")
        for article in articles:
            # find() matches tag names, so a CSS class has to go through class_.
            title_tag = article.find(["h1", "h2", "h3", "h4"]) or article.find(
                class_=re.compile(r"title", re.I)
            )
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            if len(title) < 5:
                continue

            link_tag = article.find("a", href=True)
            link = urljoin(current_url, link_tag["href"]) if link_tag else current_url

            content_div = article.find(["div", "p"]) or article.find(
                class_=re.compile(r"content|description", re.I)
            )
            content = content_div.get_text(strip=True)[:500] if content_div else ""

            data = {"title": title, "content": content}
            record_id = f"html_{hashlib.md5((link + title).encode(), usedforsecurity=False).hexdigest()[:16]}"

            records.append(ExtractedRecord(
                record_id=record_id,
                record_type="article",
                source_url=current_url,
                canonical_url=link,
                data=data,
                content_hash=hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
            ))

        # List item detection (generic listings, tables)
        if not records:
            list_items = soup.find_all(["li", "tr"], class_=re.compile(r"(item|product|listing|row|entry)", re.I))
            for item in list_items:
                title_tag = item.find(["h2", "h3", "h4", "a"]) or item.find(
                    class_=re.compile(r"title|name", re.I)
                )
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

                record_id = f"html_{hashlib.md5((link + title).encode(), usedforsecurity=False).hexdigest()[:16]}"
                records.append(ExtractedRecord(
                    record_id=record_id,
                    record_type="listing",
                    source_url=current_url,
                    canonical_url=link,
                    data=data,
                    content_hash=hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
                ))

        return records

    def _extract_overlay(self, soup: BeautifulSoup, current_url: str, overlay: Dict[str, Any]) -> List[ExtractedRecord]:
        """
        Processes declarative extraction mappings.
        Expected schema: {
            "entity_type": "generic",
            "container": ".selector",
            "mapping": { "field": ".selector" }
        }
        """
        records = []
        container_sel = overlay.get("container")
        mapping = overlay.get("mapping", {})

        if not container_sel:
            return []

        containers = soup.select(container_sel)
        for cont in containers:
            try:
                data = {"source_url": current_url}
                for field, selector in mapping.items():
                    elem = cont.select_one(selector)
                    if elem:
                        if selector.endswith("[href]"):
                            data[field] = urljoin(current_url, elem["href"])
                        elif selector.endswith("[src]"):
                            data[field] = urljoin(current_url, elem["src"])
                        else:
                            data[field] = elem.get_text(strip=True)

                title = data.get("name") or data.get("title") or "unknown"
                link = data.get("url", current_url)
                record_id = f"ov_{hashlib.md5((link + str(title)).encode(), usedforsecurity=False).hexdigest()[:16]}"

                records.append(ExtractedRecord(
                    record_id=record_id,
                    record_type=overlay.get("entity_type", "generic"),
                    source_url=current_url,
                    canonical_url=str(link),
                    data=data,
                    content_hash=hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
                ))
            except Exception as e:
                logger.debug(f"Overlay extraction error: {e}")

        return records
