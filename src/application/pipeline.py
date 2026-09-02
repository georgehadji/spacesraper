# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Application Pipeline)
# Role: Orchestrates the core ETL logic with advanced fuzzy deduplication for procurement.

import hashlib
import logging
import math
import re
import uuid
from collections import defaultdict
from typing import Any

from thefuzz import fuzz

from src.application.exploration_policy import ExplorationPolicy
from src.application.llm_metrics import groundedness
from src.domain.exceptions import ExtractionError
from src.domain.models import (
    BaseEntity,
    FollowLink,
    Opportunity,
    ProcessingResult,
    RawScrapePayload,
    StrategyObservation,
)
from src.extractors.base_extractor import BaseExtractionStrategy
from src.infrastructure.providers.enrichment_provider import EnrichmentProvider, NoOpEnrichmentProvider

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
    
    def __init__(
        self,
        ai_enrichment_enabled: bool = False,
        enrichment_provider: EnrichmentProvider | None = None,
        exploration_policy: ExplorationPolicy | None = None,
        observation_repo=None,
    ):
        self.ai_enrichment_enabled = ai_enrichment_enabled
        # Injected port — defaults to NoOp so DataPipeline is constructible with
        # no network and no singleton (contract tests run against NoOp too).
        self.enrichment_provider: EnrichmentProvider = enrichment_provider or NoOpEnrichmentProvider()
        # Bounds how often the llm_extract path actually runs (Task 5.3),
        # independent of ai_enrichment_enabled — keeps AI cost bounded and
        # gives the evaluator a steady trickle of llm_extract observations
        # to score, same 5% default used elsewhere for strategy exploration.
        self.exploration_policy = exploration_policy or ExplorationPolicy()
        # Optional SqliteObservationRepository-shaped object (create_observation).
        # No formal port — matches the rest of the codebase's convention of
        # passing this repository concretely (see worker_scraper.py).
        self.observation_repo = observation_repo

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
                    await self._enrich_opportunity(entity, payload.job_id)     # AI may now modify entity.title etc.
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

    async def _enrich_opportunity(self, entity: Opportunity, job_id: str = ""):
        """Enrich opportunity with AI-powered translation."""
        if not self.ai_enrichment_enabled or not await self.enrichment_provider.is_available():
            return

        domain = entity.source
        if not self.exploration_policy.should_explore(domain):
            return

        # Raw fields BEFORE enrichment — both the enrich() input and, for
        # groundedness, the source text the LLM's claims must trace back to.
        raw_fields = entity.model_dump()
        enrich_data = await self.enrichment_provider.enrich(raw_fields)

        success = bool(enrich_data)
        if enrich_data:
            if enrich_data.get('title_en'):
                entity.title = enrich_data['title_en']
            if enrich_data.get('buyer_en'):
                entity.buyer = enrich_data['buyer_en']
            if enrich_data.get('summary'):
                entity.summary = enrich_data['summary']
            if enrich_data.get('normalized_budget_eur') is not None:
                entity.normalized_budget_eur = enrich_data['normalized_budget_eur']

        await self._record_llm_extract_observation(job_id, domain, raw_fields, enrich_data, success)

    async def _record_llm_extract_observation(
        self, job_id: str, domain: str, raw_fields: dict[str, Any],
        enrich_data: dict[str, Any] | None, success: bool,
    ):
        """
        Records an llm_extract StrategyObservation so StrategyEvaluator can
        score this path with its existing score/recommendation machinery, and
        the llm_groundedness SLO can catch a regressing prompt or model.
        Never raises — an observation-recording failure must not break
        extraction (same pattern as worker_scraper.py's fetch-side recording).
        """
        if self.observation_repo is None:
            return

        claims = [v for v in (enrich_data or {}).values() if isinstance(v, str) and v.strip()]
        sources = [v for v in raw_fields.values() if isinstance(v, str) and v.strip()]
        score = groundedness(claims, sources)

        try:
            obs = StrategyObservation(
                observation_id=f"obs_{uuid.uuid4().hex[:12]}",
                job_id=job_id,
                domain=domain,
                strategy="llm_extract",
                valid_record_count=1 if success else 0,
                required_field_completeness=1.0 if success else 0.0,
                success=success,
                groundedness=score,
            )
            await self.observation_repo.create_observation(obs)
        except Exception as e:
            logger.debug("Failed to record llm_extract observation: %s", e)

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

    def _filter_follow_links(self, follow_links: list[FollowLink]) -> list[dict[str, Any]]:
        """Filter FollowLinks by depth to prevent discovery loops."""
        valid_urls = []
        for f in follow_links:
            if f.depth <= 3:  # Hard constraint on recursive discovery
                valid_urls.append({"url": f.url, "target_site": f.target_site, "depth": f.depth})
            else:
                logger.warning(f"Spacescraper: Discovery budget exceeded for {f.url} (Depth: {f.depth})")
        return valid_urls

    def _cluster_deduplicates_optimized(self, opportunities: list[Opportunity]) -> list[Opportunity]:
        """
        Optimized Fuzzy Deduplication Engine - O(n log n) complexity.
        Uses indexing by key attributes for faster duplicate detection.
        """
        if not opportunities:
            return []
        
        unique_results = []
        
        # Index 1: Exact URL/ID matches (O(1) lookup)
        url_index: dict[str, Opportunity] = {}
        id_index: dict[str, Opportunity] = {}
        
        # Index 2: Buyer-based groups for faster candidate filtering
        buyer_groups: dict[str | None, list[Opportunity]] = defaultdict(list)
        
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

    def _get_similarity_candidates(self, opportunity: Opportunity, buyer_groups: dict[str | None, list[Opportunity]]) -> list[Opportunity]:
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
        Determine if two opportunities are similar using fuzzy matching.
        """
        # Fuzzy title matching
        similarity = fuzz.ratio(t1.title.lower(), t2.title.lower())
        if similarity >= self.FUZZY_THRESHOLD:
            # Additional validation: buyer or deadline should match
            if t1.buyer == t2.buyer or t1.deadline == t2.deadline:
                return True
        
        return False
