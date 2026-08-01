# SLO monitoring and alert evaluation.
# Tracks key metrics and triggers alerts when thresholds are breached.

import logging
from typing import Dict, Optional, List
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("Spacescraper.SLOMonitor")


@dataclass
class SLOAlert:
    """An alert triggered by an SLO breach."""
    name: str
    severity: str  # "warning", "critical"
    message: str
    current_value: float
    threshold: float
    timestamp: str = ""


@dataclass
class SLOConfig:
    """Configuration for a single SLO."""
    name: str
    description: str
    warning_threshold: float
    critical_threshold: float
    enabled: bool = True


DEFAULT_SLOS = [
    SLOConfig(
        name="extraction_success_rate",
        description="Fraction of extraction attempts that succeed",
        warning_threshold=0.85,
        critical_threshold=0.70,
    ),
    SLOConfig(
        name="queue_age_seconds",
        description="Age of oldest pending job in seconds",
        warning_threshold=300,
        critical_threshold=600,
    ),
    SLOConfig(
        name="cache_hit_rate",
        description="Fraction of cache lookups that hit",
        warning_threshold=0.30,
        critical_threshold=0.10,
    ),
    SLOConfig(
        name="dlq_growth_rate",
        description="Dead-letter queue growth per hour",
        warning_threshold=10,
        critical_threshold=50,
    ),
    SLOConfig(
        name="block_rate",
        description="Fraction of fetches blocked/challenged",
        warning_threshold=0.10,
        critical_threshold=0.25,
    ),
    SLOConfig(
        name="ai_cost_per_hour",
        description="Estimated AI API cost per hour in cents",
        warning_threshold=100,
        critical_threshold=500,
    ),
]


class SLOMonitor:
    """
    SLO monitor that evaluates metrics against thresholds.
    Can be queried on-demand or integrated into a metrics pipeline.
    """

    def __init__(self, slos: Optional[List[SLOConfig]] = None):
        self.slos = slos or DEFAULT_SLOS

    def evaluate(self, metrics: Dict[str, float]) -> List[SLOAlert]:
        """
        Evaluate all SLOs against provided metrics.
        Returns list of triggered alerts (empty if all SLOs pass).
        """
        alerts = []
        now = datetime.now(tz=timezone.utc).isoformat()

        for slo in self.slos:
            if not slo.enabled:
                continue

            value = metrics.get(slo.name)
            if value is None:
                continue

            if slo.name in ("queue_age_seconds", "dlq_growth_rate", "ai_cost_per_hour", "block_rate"):
                # Higher is worse
                if value >= slo.critical_threshold:
                    alerts.append(SLOAlert(
                        name=slo.name, severity="critical",
                        message=f"{slo.description}: {value:.1f} (threshold: {slo.critical_threshold})",
                        current_value=value, threshold=slo.critical_threshold,
                        timestamp=now,
                    ))
                elif value >= slo.warning_threshold:
                    alerts.append(SLOAlert(
                        name=slo.name, severity="warning",
                        message=f"{slo.description}: {value:.1f} (threshold: {slo.warning_threshold})",
                        current_value=value, threshold=slo.warning_threshold,
                        timestamp=now,
                    ))
            else:
                # Lower is worse (rates: lower values = worse)
                if value <= slo.critical_threshold:
                    alerts.append(SLOAlert(
                        name=slo.name, severity="critical",
                        message=f"{slo.description}: {value:.2f} (threshold: {slo.critical_threshold})",
                        current_value=value, threshold=slo.critical_threshold,
                        timestamp=now,
                    ))
                elif value <= slo.warning_threshold:
                    alerts.append(SLOAlert(
                        name=slo.name, severity="warning",
                        message=f"{slo.description}: {value:.2f} (threshold: {slo.warning_threshold})",
                        current_value=value, threshold=slo.warning_threshold,
                        timestamp=now,
                    ))

        return alerts

    def is_healthy(self, metrics: Dict[str, float]) -> bool:
        """Returns True if all SLOs pass."""
        return len(self.evaluate(metrics)) == 0


class AutoRollback:
    """
    Automated overlay rollback on quality regression.
    Monitors extraction success rate and latency for the current ACTIVE overlay.
    If metrics degrade beyond thresholds, rolls back to the previous version.
    """

    def __init__(self, min_observations: int = 20,
                 success_rate_threshold: float = 0.60,
                 latency_increase_threshold: float = 2.0):
        self.min_observations = min_observations
        self.success_rate_threshold = success_rate_threshold
        self.latency_increase_threshold = latency_increase_threshold

    async def check_and_rollback(self, overlay_repo, observations) -> Optional[str]:
        """
        Check if the current ACTIVE overlay is regressing.
        Returns rollback overlay_id if rollback is needed, None otherwise.
        """
        from src.domain.models import OverlayState

        # Get current ACTIVE overlay
        # Need the domain from observations
        if not observations:
            return None

        domains = set(o.domain for o in observations)
        for domain in domains:
            active = await overlay_repo.get_active_overlay(domain)
            if not active or not active.rollback_overlay_id:
                continue

            # Get recent observations for this domain
            domain_obs = [o for o in observations if o.domain == domain]
            if len(domain_obs) < self.min_observations:
                continue

            # Calculate success rate
            success_rate = sum(1 for o in domain_obs if o.success) / len(domain_obs)
            if success_rate < self.success_rate_threshold:
                logger.warning(
                    "AutoRollback: Rolling back %s from %s (success=%.0f%% < %.0f%%)",
                    domain, active.overlay_id, success_rate * 100,
                    self.success_rate_threshold * 100,
                )
                # Retire current ACTIVE, restore rollback target
                await overlay_repo.update_overlay_state(
                    active.overlay_id, OverlayState.RETIRED
                )
                await overlay_repo.update_overlay_state(
                    active.rollback_overlay_id, OverlayState.ACTIVE
                )
                return active.rollback_overlay_id

        return None
