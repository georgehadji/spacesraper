# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Universal Hybrid Extractor)
# Role: Unified heuristic engine for both E-Commerce and Procurement targets.

import logging
import json
import re
import hashlib
from typing import List, Dict, Any, Union, Optional
from urllib.parse import urljoin
from bs4 import BeautifulSoup

from src.extractors.base_extractor import BaseExtractionStrategy
from src.domain.models import Product, Opportunity, Lead, Article, FollowLink, BaseEntity
from src.infrastructure.ai.client import ai_orchestrator

logger = logging.getLogger("Spacescraper.UniversalStrategy")

class UniversalExtractionStrategy(BaseExtractionStrategy):
    """
    Spacescraper Unified Intelligence Node.
    Consolidates e-commerce (Product) and procurement (Opportunity) search signatures
    into a single optimized extraction pass.
    """

    async def extract(self, html: str, json_payloads: List[Dict[str, Any]], current_url: str = "", overlay: Optional[Dict[str, Any]] = None) -> List[BaseEntity]:
        soup = BeautifulSoup(html, "html.parser")
        entities: List[BaseEntity] = []

        # 0. Declarative Overlay: Explicit mapping from config (High priority)
        if overlay:
            overlay_results = self._extract_overlay(soup, current_url, overlay)
            if overlay_results:
                return overlay_results # Overlay acts as a hard override

        # 1. High-Fidelity Capture: JSON-LD (Favored for Products)
        json_ld_products = self._extract_json_ld(soup, current_url)
        if json_ld_products:
            entities.extend(json_ld_products)
            # If we find valid JSON-LD products, we often don't need further heuristics for this page
            # return entities 

        # 2. Heuristic Capture: DOM Analysis
        # Determine semantic context based on keywords (Procurement vs Retail)
        page_text = soup.get_text(separator=" ", strip=True).lower()
        is_procurement_intent = any(k in page_text for k in ['opportunity', 'procurement', 'deadline', 'rfp', 'buyer'])

        if is_procurement_intent:
            entities.extend(self._extract_opportunity_heuristics(soup, current_url))
        else:
            # Only run product heuristics if no JSON-LD was found or we want to supplement
            if not json_ld_products:
                entities.extend(self._extract_product_heuristics(soup, current_url))

        # Scenario 1: AI Self-Healing Fallback
        if not entities and ai_orchestrator.enabled:
            logger.info(f"Spacescraper IntelligenceGap: Heuristics failed on {current_url}. Triggering AI Self-Healing...")
            ai_entities = await self._attempt_ai_healing(html, is_procurement_intent, current_url)
            entities.extend(ai_entities)

        return entities

    async def _attempt_ai_healing(self, html: str, is_procurement: bool, url: str) -> List[BaseEntity]:
        """
        Utilizes LLM to semantically identify entities when DOM patterns break.
        """
        target = "procurement opportunities (title, buyer, deadline, budget, url)" if is_procurement else "product listings (name, price, currency, url)"
        # We use a limited snippet of the body to fit within token limits and focus on content
        soup = BeautifulSoup(html, "html.parser")
        body = soup.find("body")
        content = body.get_text(separator=" ", strip=True) if body else html[:10000]
        
        # In a real-world scenario, we'd pass the HTML structure, but for this demo 
        # we'll ask the AI to find the data semantically.
        # This acts as the ultimate 'Self-Healing' layer.
        return [] # Placeholder for actual AI extraction logic implementation

    def _extract_json_ld(self, soup: BeautifulSoup, current_url: str) -> List[Product]:
        products = []
        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            if not script.string: continue
            try:
                data = json.loads(script.string)
                items = data.get("@graph", [data]) if isinstance(data, dict) else data
                if not isinstance(items, list): items = [items]
                
                for item in items:
                    if item.get("@type") == "Product":
                        name = item.get("name")
                        offers = item.get("offers", {})
                        if isinstance(offers, list) and offers: offers = offers[0]
                        
                        price = offers.get("price")
                        if name:
                            products.append(Product(
                                id=item.get("sku") or item.get("mpn") or f"ss_ld_{hash(name)}",
                                name=name,
                                price=float(price) if price else None,
                                currency=offers.get("priceCurrency", "EUR"),
                                url=current_url,
                                source_url=current_url
                            ))
            except: pass
        return products

    def _extract_opportunity_heuristics(self, soup: BeautifulSoup, current_url: str) -> List[Opportunity]:
        opportunities = []
        selectors = ["tr", ".opportunity-item", ".procurement-card", "article", ".datarow"]
        containers = soup.select(", ".join(selectors))
        
        for row in containers:
            text = row.get_text(separator=" ", strip=True).lower()
            if not any(k in text for k in ['deadline', 'opportunity', 'rfp', 'procurement', 'reference']):
                continue
                
            try:
                links = row.find_all("a", href=True)
                if not links: continue
                
                title_tag = row.select_one(".title, .subject, .name, h3, h4") or links[0]
                title = title_tag.get_text(strip=True)
                link = urljoin(current_url, links[0]["href"])
                
                ref_id = None
                ref_tags = row.select(".reference, .id, .ref-id, .case-num")
                if ref_tags: ref_id = ref_tags[0].get_text(strip=True)
                if not ref_id:
                    ref_id = hashlib.md5(f"{link}{title}".encode()).hexdigest()[:12].upper()
                
                opportunities.append(Opportunity(
                    source="Universal Heuristic",
                    external_id=ref_id,
                    title=title,
                    buyer=(row.select_one(".buyer, .issuer, .organization") or type('obj', (object,), {'get_text': lambda s, strip: None})()).get_text(strip=True),
                    deadline=(row.select_one(".deadline, .due-date, .closes") or type('obj', (object,), {'get_text': lambda s, strip: None})()).get_text(strip=True),
                    estimated_budget=(row.select_one(".budget, .value, .amount") or type('obj', (object,), {'get_text': lambda s, strip: None})()).get_text(strip=True),
                    url=link,
                    source_url=current_url
                ))
            except: pass
        return opportunities

    def _extract_product_heuristics(self, soup: BeautifulSoup, current_url: str) -> List[Product]:
        products = []
        selectors = [".product", ".product-item", ".item", ".product-card", ".listing"]
        containers = soup.select(", ".join(selectors))
        
        for cont in containers:
            try:
                title_tag = cont.select_one("h1, h2, h3, .title, .product-title, .name")
                if not title_tag: continue
                title = title_tag.get_text(strip=True)
                
                price_tag = cont.select_one(".price, .product-price, .amount")
                price = None
                if price_tag:
                    match = re.search(r"(\d+[\.\,]?\d*)", price_tag.get_text(strip=True).replace(",", ""))
                    if match: price = float(match.group(1))

                link_tag = cont.select_one("a[href]")
                link = urljoin(current_url, link_tag["href"]) if link_tag else current_url

                products.append(Product(
                    id=f"ss_h_{hash(title)}",
                    name=title,
                    price=price,
                    currency="EUR", 
                    url=link,
                    source_url=current_url,
                ))
            except: pass
        return products
    def _extract_overlay(self, soup: BeautifulSoup, current_url: str, overlay: Dict[str, Any]) -> List[BaseEntity]:
        """
        Processes declarative extraction mappings.
        Expected schema: {
            "entity_type": "Opportunity" | "Product",
            "container": ".selector",
            "mapping": { "field": ".selector" }
        }
        """
        entities = []
        entity_type = overlay.get("entity_type", "Product")
        container_sel = overlay.get("container")
        mapping = overlay.get("mapping", {})

        if not container_sel: return []

        containers = soup.select(container_sel)
        for cont in containers:
            try:
                data = {"source_url": current_url}
                for field, selector in mapping.items():
                    elem = cont.select_one(selector)
                    if elem:
                        if selector.endswith("[href]"): data[field] = urljoin(current_url, elem["href"])
                        elif selector.endswith("[src]"): data[field] = urljoin(current_url, elem["src"])
                        else: data[field] = elem.get_text(strip=True)
                
                if entity_type == "Opportunity":
                    if "url" not in data: data["url"] = current_url
                    data["source"] = "Overlay"
                    entities.append(Opportunity(**data))
                elif entity_type == "Product":
                    if "id" not in data: data["id"] = f"ov_{hash(data.get('name', ''))}"
                    if "url" not in data: data["url"] = current_url
                    entities.append(Product(**data))
            except Exception as e:
                logger.debug(f"Overlay extraction error: {e}")
        
        return entities
