# Shadow overlay evaluator — runs candidate overlays in shadow mode
# and compares their output against the ACTIVE overlay without
# affecting production behavior.

import uuid
import hashlib
import logging
from typing import Optional, List, Tuple
from bs4 import BeautifulSoup

from src.domain.models import ExtractionOverlay, OverlayState, ExtractedRecord, EvaluationResult
from src.infrastructure.repositories.overlay_repository import SqliteOverlayRepository
from src.application.extraction_pipeline import DeterministicExtractionPipeline
from src.application.evaluator import StrategyEvaluator

logger = logging.getLogger("Spacescraper.ShadowEvaluator")


class ShadowOverlayEvaluator:
    """
    Evaluates candidate overlays in shadow mode.
    Runs both the candidate and ACTIVE overlay on the same HTML,
    compares their output quality, and produces an EvaluationResult.
    """

    def __init__(self, overlay_repo: SqliteOverlayRepository, evaluator: StrategyEvaluator):
        self.overlay_repo = overlay_repo
        self.evaluator = evaluator
        self.pipeline = DeterministicExtractionPipeline(overlay_repo=overlay_repo)

    async def evaluate_candidate(
        self, candidate_overlay_id: str, html_samples: List[Tuple[str, str]],
    ) -> Optional[EvaluationResult]:
        """
        Evaluate a candidate overlay against the ACTIVE overlay (if any).
        
        Args:
            candidate_overlay_id: The CANDIDATE overlay to evaluate.
            html_samples: List of (url, html_content) pairs to test against.
            
        Returns:
            EvaluationResult with comparison metrics, or None if evaluation cannot proceed.
        """
        candidate = await self.overlay_repo.get_overlay(candidate_overlay_id)
        if not candidate:
            logger.warning("ShadowEvaluator: Candidate overlay %s not found", candidate_overlay_id)
            return None

        if candidate.state not in (OverlayState.CANDIDATE, OverlayState.SHADOW):
            logger.warning("ShadowEvaluator: Overlay %s is not in CANDIDATE/SHADOW state", candidate_overlay_id)
            return None

        domain = candidate.domain
        active = await self.overlay_repo.get_active_overlay(domain)

        candidate_records = []
        active_records = []

        for url, html in html_samples:
            # Run with candidate overlay
            candidate_overlay_dict = {
                "entity_type": candidate.schema_id,
                "container": candidate.container_selector or "",
                "mapping": candidate.field_mappings,
            }
            cand_results = await self.pipeline.extract(html, [], url, overlay=candidate_overlay_dict)
            candidate_records.extend(cand_results)

            # Run with ACTIVE overlay (if exists)
            if active:
                active_overlay_dict = {
                    "entity_type": active.schema_id,
                    "container": active.container_selector or "",
                    "mapping": active.field_mappings,
                }
                act_results = await self.pipeline.extract(html, [], url, overlay=active_overlay_dict)
                active_records.extend(act_results)

        if not candidate_records:
            logger.warning("ShadowEvaluator: Candidate produced no records")
            return None

        # Compute metrics
        cand_precision = len(candidate_records) / max(len(html_samples), 1)
        cand_completeness = sum(1 for r in candidate_records if r.data) / max(len(candidate_records), 1)

        if active_records:
            baseline_precision = len(active_records) / max(len(html_samples), 1)
            score_improvement = cand_precision - baseline_precision
        else:
            baseline_precision = 0
            score_improvement = cand_precision

        # Determine recommendation
        if score_improvement > 0.1:
            recommendation = "promote"
        elif score_improvement < -0.1:
            recommendation = "demote"
        else:
            recommendation = "no_change"

        result = EvaluationResult(
            evaluation_id=f"eval_{uuid.uuid4().hex[:12]}",
            candidate_strategy=f"overlay:{candidate.overlay_id}",
            baseline_strategy=f"overlay:{active.overlay_id if active else 'none'}",
            domain=domain,
            sample_size=len(html_samples),
            precision=cand_precision,
            completeness=cand_completeness,
            latency_p50=0,  # not measured in shadow mode
            latency_p95=0,
            cost_per_record=0,
            block_rate=0,
            score=min(1.0, cand_precision),
            recommendation=recommendation,
        )

        await self.evaluator.repo.create_evaluation(result)
        logger.info("ShadowEvaluator: %s -> %s (score=%.2f, rec=%s)",
                    candidate_overlay_id, domain, result.score, recommendation)
        return result

    async def promote_to_shadow(self, overlay_id: str) -> Optional[ExtractionOverlay]:
        """Promote a CANDIDATE overlay to SHADOW state."""
        overlay = await self.overlay_repo.get_overlay(overlay_id)
        if not overlay or overlay.state != OverlayState.CANDIDATE:
            return None
        return await self.overlay_repo.update_overlay_state(overlay_id, OverlayState.SHADOW)

    async def promote_to_active(self, overlay_id: str) -> Optional[ExtractionOverlay]:
        """
        Promote a SHADOW overlay to ACTIVE.
        Retires the previous ACTIVE overlay.
        Requires minimum evidence: 20 successful validations.
        """
        overlay = await self.overlay_repo.get_overlay(overlay_id)
        if not overlay or overlay.state != OverlayState.SHADOW:
            return None

        # Check for sufficient evidence
        evaluations = await self._get_recent_evaluations(overlay_id, min_count=20)
        if not evaluations:
            logger.warning("ShadowEvaluator: Insufficient evidence to promote %s to ACTIVE", overlay_id)
            return None

        # Retire previous ACTIVE
        active = await self.overlay_repo.get_active_overlay(overlay.domain)
        if active:
            await self.overlay_repo.update_overlay_state(active.overlay_id, OverlayState.RETIRED)

        return await self.overlay_repo.update_overlay_state(overlay_id, OverlayState.ACTIVE)

    async def _get_recent_evaluations(self, overlay_id: str, min_count: int = 20) -> bool:
        """Check if there are enough recent positive evaluations for this overlay."""
        from datetime import datetime, timedelta
        cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
        evaluations = await self.evaluator.repo.get_observations(limit=100)
        count = sum(1 for e in evaluations if hasattr(e, 'overlay_id') and
                    getattr(e, 'overlay_id', None) == overlay_id)
        return count >= min_count
