# Offline evaluator — compares strategy quality across domains.
# Reads StrategyObservations, computes quality metrics, produces EvaluationResults.

import uuid
import logging
from datetime import datetime, timedelta
from typing import List, Optional
from src.domain.models import StrategyObservation, EvaluationResult, DomainProfile
from src.domain.ports import ObservationRepository

logger = logging.getLogger("Spacescraper.Evaluator")

MIN_OBSERVATIONS_FOR_EVALUATION = 5


class StrategyEvaluator:
    """
    Offline evaluator that compares extraction strategies.
    Reads observations from the repository, computes quality metrics,
    and produces EvaluationResults with promotion/demotion recommendations.
    """

    def __init__(self, repo: ObservationRepository):
        self.repo = repo

    async def evaluate_strategy(
        self, domain: str, candidate_strategy: str,
        baseline_strategy: str = "http",
        max_age_hours: int = 168,  # 7 days
    ) -> Optional[EvaluationResult]:
        """
        Compare a candidate strategy against the baseline for a domain.
        Returns an EvaluationResult with quality metrics and recommendation.
        """
        cutoff = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat()

        # Fetch observations for both strategies
        candidate_obs = await self._get_observations_since(domain, candidate_strategy, cutoff)
        baseline_obs = await self._get_observations_since(domain, baseline_strategy, cutoff)

        if len(candidate_obs) < MIN_OBSERVATIONS_FOR_EVALUATION:
            logger.info("Evaluator: Not enough candidate observations for %s/%s (%d < %d)",
                       domain, candidate_strategy, len(candidate_obs), MIN_OBSERVATIONS_FOR_EVALUATION)
            return None

        metrics = self._compute_metrics(candidate_obs, baseline_obs)
        if metrics is None:
            return None

        result = EvaluationResult(
            evaluation_id=f"eval_{uuid.uuid4().hex[:12]}",
            candidate_strategy=candidate_strategy,
            baseline_strategy=baseline_strategy,
            domain=domain,
            sample_size=len(candidate_obs),
            precision=metrics["precision"],
            completeness=metrics["completeness"],
            latency_p50=metrics["latency_p50"],
            latency_p95=metrics["latency_p95"],
            cost_per_record=metrics["cost_per_record"],
            block_rate=metrics["block_rate"],
            score=metrics["score"],
            recommendation=metrics["recommendation"],
        )

        await self.repo.create_evaluation(result)
        return result

    async def update_domain_profile(self, domain: str) -> Optional[DomainProfile]:
        """
        Recompute the domain profile based on recent observations.
        Selects the best strategy by composite score.
        """
        strategies = ["http", "browser", "overlay", "json_ld", "semantic_html"]
        best_score = -1.0
        best_strategy = "http"

        for strategy in strategies:
            obs = await self._get_observations_since(domain, strategy)
            if len(obs) < MIN_OBSERVATIONS_FOR_EVALUATION:
                continue

            metrics = self._compute_strategy_metrics(obs)
            score = self._compute_score(
                precision=metrics["precision"],
                completeness=metrics["completeness"],
                latency_ms=metrics["avg_latency"],
                block_rate=metrics["block_rate"],
            )
            if score > best_score:
                best_score = score
                best_strategy = strategy

        profile = await self.repo.get_or_create_profile(domain)
        profile.preferred_strategy = best_strategy
        profile.total_observations += 1
        profile.last_observed = datetime.utcnow()

        # Update aggregate metrics from latest observations
        recent = await self._get_observations_since(domain, "", hours=24)
        if recent:
            success_count = sum(1 for o in recent if o.success)
            profile.success_rate = success_count / len(recent)
            profile.avg_latency_ms = sum(o.latency_ms for o in recent) / len(recent)
            profile.block_rate = sum(1 for o in recent if o.blocked) / len(recent)

        await self.repo.update_profile(profile)
        return profile

    async def _get_observations_since(
        self, domain: str, strategy: str = "", hours: int = 168, cutoff: str = ""
    ) -> List[StrategyObservation]:
        cutoff_str = cutoff or (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        obs = await self.repo.get_observations(domain=domain, limit=500)
        filtered = [o for o in obs if o.created_at.isoformat() >= cutoff_str]
        if strategy:
            filtered = [o for o in filtered if o.strategy == strategy]
        return filtered

    def _compute_metrics(
        self, candidate: List[StrategyObservation], baseline: List[StrategyObservation]
    ) -> Optional[dict]:
        if not candidate:
            return None

        c_metrics = self._compute_strategy_metrics(candidate)
        b_metrics = self._compute_strategy_metrics(baseline) if baseline else c_metrics

        score = self._compute_score(
            precision=c_metrics["precision"],
            completeness=c_metrics["completeness"],
            latency_ms=c_metrics["avg_latency"],
            block_rate=c_metrics["block_rate"],
        )

        baseline_score = self._compute_score(
            precision=b_metrics["precision"],
            completeness=b_metrics["completeness"],
            latency_ms=b_metrics["avg_latency"],
            block_rate=b_metrics["block_rate"],
        )

        # Determine recommendation
        if score > baseline_score * 1.1:
            recommendation = "promote"
        elif score < baseline_score * 0.9:
            recommendation = "demote"
        else:
            recommendation = "no_change"

        latencies = sorted(o.latency_ms for o in candidate)
        n = len(latencies)

        return {
            "precision": c_metrics["precision"],
            "completeness": c_metrics["completeness"],
            "latency_p50": latencies[n // 2] if n > 0 else 0,
            "latency_p95": latencies[int(n * 0.95)] if n > 0 else 0,
            "cost_per_record": c_metrics["avg_cost"],
            "block_rate": c_metrics["block_rate"],
            "score": score,
            "recommendation": recommendation,
        }

    @staticmethod
    def _compute_strategy_metrics(obs: List[StrategyObservation]) -> dict:
        if not obs:
            return {"precision": 0, "completeness": 0, "avg_latency": 0, "block_rate": 0, "avg_cost": 0}
        n = len(obs)
        return {
            "precision": sum(1 for o in obs if o.valid_record_count > 0) / n,
            "completeness": sum(o.required_field_completeness for o in obs) / n,
            "avg_latency": sum(o.latency_ms for o in obs) / n,
            "block_rate": sum(1 for o in obs if o.blocked) / n,
            "avg_cost": sum(o.cost for o in obs) / n,
        }

    @staticmethod
    def _compute_score(precision: float, completeness: float,
                       latency_ms: float, block_rate: float,
                       precision_weight: float = 0.5, completeness_weight: float = 0.2,
                       latency_weight: float = 0.15, block_weight: float = 0.15) -> float:
        """Compute a composite utility score (0-1). Higher is better."""
        # Normalize latency: 0 = 10s+, 1 = 100ms or less
        latency_score = max(0.0, min(1.0, 1.0 - (latency_ms / 10000.0)))
        block_score = 1.0 - block_rate
        return (
            precision * precision_weight +
            completeness * completeness_weight +
            latency_score * latency_weight +
            block_score * block_weight
        )
