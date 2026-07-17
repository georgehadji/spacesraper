# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Application Pipeline)
# Role: Orchestrates the core ETL logic with advanced fuzzy deduplication for procurement.

import logging
import uuid
import hashlib
import math
import re
from typing import List, Dict, Any, Union, Optional, Tuple
from collections import defaultdict
from thefuzz import fuzz
from src.domain.models import RawScrapePayload, ProcessingResult, BaseEntity, Opportunity, FollowLink
from src.extractors.base_extractor import BaseExtractionStrategy
from src.domain.exceptions import ExtractionError
from src.infrastructure.ai.client import ai_orchestrator

logger = logging.getLogger("Spacescraper.Pipeline")

class IntegrityAuditResult:
    """Signals if an entity passed semantic validation."""
    def __init__(self, passed: bool, reason: str = ""):
        self.passed = passed
        self.reason = reason

class DataPipeline:
    """
    Spacescraper Intelligence Orchestrator.
    Handles extraction, fuzzy deduplication, and structured entity resolution.
    """
    
    # Similarity thresholds
    FUZZY_THRESHOLD = 90
    COSINE_THRESHOLD = 0.85
    
    def __init__(self, ai_enrichment_enabled: bool = False):
        self.ai_enrichment_enabled = ai_enrichment_enabled
        # Embedding cache for deduplication within a batch
        self._embedding_cache: Dict[str, List[float]] = {}

    async def process(self, payload: RawScrapePayload, strategy: BaseExtractionStrategy) -> ProcessingResult:
        """Executes the transformation cycle for a raw ingestion package."""
        result = ProcessingResult(job_id=payload.job_id, success=False)
        
        if payload.status_code >= 400 or payload.error_message:
            result.error = payload.error_message
            return result
            
        try:
            # Stage 1: Component Extraction
            logger.info(f"Spacescraper: Dispatching to {strategy.__class__.__name__}")
            extracted_entities = await strategy.extract(
                payload.html_content, 
                payload.json_payloads,
                current_url=payload.url,
                overlay=payload.overlay
            )
            
            # Stage 2: Entity Lifecycle Management (Normalization & Hash)
            opportunities = []
            non_opportunities = []
            
            for entity in extracted_entities:
                if isinstance(entity, Opportunity):
                    entity.source = payload.target_site
                    self._compute_identity_hash(entity)   # Raw fields — must be before AI enrichment
                    await self._enrich_opportunity(entity)     # AI may now modify entity.title etc.
                    self._compute_content_hash(entity)
                    self._audit_integrity(entity)
                    opportunities.append(entity)
                elif isinstance(entity, FollowLink):
                    # Depth Management for discovery
                    entity.depth = payload.depth + 1
                    non_opportunities.append(entity)
                else:
                    non_opportunities.append(entity)

            # Stage 3: Professional Fuzzy Deduplication (Optimized O(n log n))
            unique_opportunities = self._cluster_deduplicates_optimized(opportunities)
            
            # Stage 4: Discovery Categorization & Constraints
            follow_links = [e for e in non_opportunities if isinstance(e, FollowLink)]
            valid_follow_urls = self._filter_follow_links(follow_links)

            result.success = True
            result.entities = unique_opportunities + [e for e in non_opportunities if not isinstance(e, FollowLink)]
            result.follow_urls = valid_follow_urls
            
        except ExtractionError as e:
            logger.error(f"Spacescraper Extraction Logic Fault: {e}")
            result.error = e.message if hasattr(e, 'message') else str(e)
            
        except Exception as e:
            logger.exception(f"Spacescraper Pipeline Critical Error: {e}")
            result.error = f"Pipeline Internal Error: {str(e)}"
            
        return result

    async def _enrich_opportunity(self, entity: Opportunity):
        """Enrich opportunity with AI-powered translation and embeddings."""
        if not ai_orchestrator.enabled:
            return
            
        # AI Translation & Normalization
        enrich_data = await ai_orchestrator.enrich_opportunity(entity.model_dump())
        if enrich_data:
            if enrich_data.get('title_en'):
                entity.title = enrich_data['title_en']
            if enrich_data.get('buyer_en'):
                entity.buyer = enrich_data['buyer_en']
            if enrich_data.get('summary'):
                entity.summary = enrich_data['summary']
            if enrich_data.get('normalized_budget_eur') is not None:
                entity.normalized_budget_eur = enrich_data['normalized_budget_eur']
        
        # Generate numerical vector for ML clustering with caching
        text_for_ml = f"{entity.title} {entity.summary or ''} {entity.buyer or ''}"
        embedding = await ai_orchestrator.compute_embedding_with_cache(text_for_ml, self._embedding_cache)
        if embedding:
            entity.embedding = embedding

    def _compute_content_hash(self, entity: Opportunity):
        """Calculate content hash for change detection."""
        sig_data = f"{entity.title}|{entity.deadline}|{entity.estimated_budget}|{entity.status}"
        entity.content_hash = hashlib.md5(sig_data.encode(), usedforsecurity=False).hexdigest()

    def _compute_identity_hash(self, entity: Opportunity):
        """
        Stable identity hash computed from raw, pre-AI fields only.
        Never changes due to AI model updates — only changes on genuine data edits.
        """
        sig = f"{entity.url}|{entity.title}|{entity.deadline or ''}"
        entity.identity_hash = hashlib.md5(sig.encode(), usedforsecurity=False).hexdigest()

    def _audit_integrity(self, entity: Opportunity):
        """Minimax Regret Guardrail - Validates extracted data follows logical business rules."""
        audit = self._audit_semantic_integrity(entity)
        if not audit.passed:
            logger.warning(f"Spacescraper Integrity Warning: {entity.external_id} marked as UNCERTAIN. Reason: {audit.reason}")
            entity.status = "UNCERTAIN"
            entity.classification = f"AUDIT_FLAG: {audit.reason}"

    def _audit_semantic_integrity(self, entity: Opportunity) -> IntegrityAuditResult:
        """
        Minimax Regret Guardrail.
        Validates that extracted data follows logical business rules.
        """
        # 1. Budget Outlier Detection
        if entity.estimated_budget:
            try:
                numeric_val = float(re.sub(r'[^\d.]', '', entity.estimated_budget.replace(",", "")))
                if numeric_val <= 0 or numeric_val > 500_000_000:
                    return IntegrityAuditResult(False, "Extreme Budget Outlier (Anomaly)")
            except ValueError:
                pass

        # 2. Date Logical Consistency
        if entity.publication_date and entity.deadline:
            if len(entity.deadline) < 5:
                return IntegrityAuditResult(False, "Malformed Deadline Format")

        # 3. Title Semantic Quality
        if len(entity.title) < 10 or entity.title.count(' ') < 1:
            return IntegrityAuditResult(False, "Low-quality title detected")

        return IntegrityAuditResult(True)

    def _filter_follow_links(self, follow_links: List[FollowLink]) -> List[Dict[str, Any]]:
        """Filter FollowLinks by depth to prevent discovery loops."""
        valid_urls = []
        for f in follow_links:
            if f.depth <= 3:  # Hard constraint on recursive discovery
                valid_urls.append({"url": f.url, "target_site": f.target_site, "depth": f.depth})
            else:
                logger.warning(f"Spacescraper: Discovery budget exceeded for {f.url} (Depth: {f.depth})")
        return valid_urls

    def _cluster_deduplicates_optimized(self, opportunities: List[Opportunity]) -> List[Opportunity]:
        """
        Optimized Fuzzy Deduplication Engine - O(n log n) complexity.
        Uses indexing by key attributes for faster duplicate detection.
        """
        if not opportunities:
            return []
        
        unique_results = []
        
        # Index 1: Exact URL/ID matches (O(1) lookup)
        url_index: Dict[str, Opportunity] = {}
        id_index: Dict[str, Opportunity] = {}
        
        # Index 2: Buyer-based groups for faster candidate filtering
        buyer_groups: Dict[Optional[str], List[Opportunity]] = defaultdict(list)
        
        for t in opportunities:
            is_duplicate = False
            duplicate_group_id = None
            
            # Check 1: Exact Identity (O(1))
            if t.url in url_index:
                is_duplicate = True
                duplicate_group_id = url_index[t.url].duplicate_group_id
            elif t.external_id and t.external_id in id_index:
                is_duplicate = True
                duplicate_group_id = id_index[t.external_id].duplicate_group_id
            else:
                # Check 2: Fuzzy/ML match within buyer group (O(k) where k is small)
                candidates = self._get_similarity_candidates(t, buyer_groups)
                
                for canonical in candidates:
                    if self._is_similar(t, canonical):
                        is_duplicate = True
                        duplicate_group_id = canonical.duplicate_group_id
                        break
            
            if is_duplicate:
                t.duplicate_group_id = duplicate_group_id
            else:
                # New unique opportunity
                new_gid = f"cluster_{uuid.uuid4().hex[:8]}"
                t.duplicate_group_id = new_gid
                unique_results.append(t)
                
                # Update indexes
                url_index[t.url] = t
                if t.external_id:
                    id_index[t.external_id] = t
                buyer_groups[t.buyer].append(t)
        
        return unique_results

    def _get_similarity_candidates(self, opportunity: Opportunity, buyer_groups: Dict[Optional[str], List[Opportunity]]) -> List[Opportunity]:
        """
        Get candidate opportunities for similarity comparison.
        Only returns opportunities from the same buyer or recent ones to reduce comparison count.
        """
        candidates = []
        
        # Primary: Same buyer group
        if opportunity.buyer and opportunity.buyer in buyer_groups:
            candidates.extend(buyer_groups[opportunity.buyer])
        
        # Secondary: Unknown buyer group (check all with no buyer)
        if None in buyer_groups:
            candidates.extend(buyer_groups[None])
        
        return candidates

    def _is_similar(self, t1: Opportunity, t2: Opportunity) -> bool:
        """
        Determine if two opportunities are similar using ML embedding or fuzzy matching.
        """
        # ML Deduplication via Cosine Similarity
        if t1.embedding and t2.embedding:
            cos_sim = self._cosine_similarity(t1.embedding, t2.embedding)
            if cos_sim >= self.COSINE_THRESHOLD:
                logger.debug(f"Spacescraper ML Match: {t1.title[:30]}... == {t2.title[:30]}... (Score: {cos_sim:.2f})")
                return True
        
        # Fallback to Fuzzy Matrix
        similarity = fuzz.ratio(t1.title.lower(), t2.title.lower())
        if similarity >= self.FUZZY_THRESHOLD:
            # Additional validation: buyer or deadline should match
            if t1.buyer == t2.buyer or t1.deadline == t2.deadline:
                return True
        
        return False

    @staticmethod
    def _cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        dot_product = sum(x * y for x, y in zip(vec1, vec2))
        norm1 = math.sqrt(sum(x * x for x in vec1))
        norm2 = math.sqrt(sum(x * x for x in vec2))
        return dot_product / (norm1 * norm2) if (norm1 * norm2) > 0 else 0.0
