# Tests for evaluator, shadow evaluator, strategy selector, SLO monitor.
# Run via: python -m pytest tests/test_increment_modules.py -v


import pytest

from src.application.evaluator import StrategyEvaluator
from src.domain.models import StrategyObservation
from src.infrastructure.slo_monitor import AutoRollback, SLOConfig, SLOMonitor


class TestStrategyEvaluator:
    """StrategyEvaluator unit tests."""

    def test_compute_score_perfect(self):
        score = StrategyEvaluator._compute_score(
            precision=1.0, completeness=1.0, latency_ms=100, block_rate=0.0
        )
        assert score > 0.9  # Near-perfect score

    def test_compute_score_poor(self):
        score = StrategyEvaluator._compute_score(
            precision=0.0, completeness=0.0, latency_ms=10000, block_rate=1.0
        )
        assert score < 0.3  # Poor score

    def test_compute_strategy_metrics_empty(self):
        metrics = StrategyEvaluator._compute_strategy_metrics([])
        assert metrics["precision"] == 0
        assert metrics["completeness"] == 0

    def test_compute_strategy_metrics_with_data(self):
        obs = [
            StrategyObservation(
                observation_id="o1", job_id="j1", domain="test.com", strategy="http",
                valid_record_count=5, required_field_completeness=0.9,
                latency_ms=100, success=True, blocked=False, cost=0.0, duplicate_rate=0.0,
            ),
            StrategyObservation(
                observation_id="o2", job_id="j1", domain="test.com", strategy="http",
                valid_record_count=0, required_field_completeness=0.0,
                latency_ms=200, success=False, blocked=True, cost=0.0, duplicate_rate=0.0,
            ),
        ]
        metrics = StrategyEvaluator._compute_strategy_metrics(obs)
        assert metrics["precision"] == 0.5  # 1 out of 2 had records
        assert metrics["block_rate"] == 0.5  # 1 out of 2 blocked
        assert metrics["avg_latency"] == 150.0


class TestSLOMonitor:
    """SLOMonitor unit tests."""

    def setup_method(self):
        self.monitor = SLOMonitor()

    def test_all_healthy(self):
        metrics = {
            "extraction_success_rate": 0.95,
            "queue_age_seconds": 5,
            "cache_hit_rate": 0.50,
            "dlq_growth_rate": 1,
            "block_rate": 0.02,
            "ai_cost_per_hour": 10,
        }
        alerts = self.monitor.evaluate(metrics)
        assert len(alerts) == 0
        assert self.monitor.is_healthy(metrics)

    def test_success_rate_warning(self):
        metrics = {"extraction_success_rate": 0.80}
        alerts = self.monitor.evaluate(metrics)
        assert len(alerts) == 1
        assert alerts[0].severity == "warning"

    def test_success_rate_critical(self):
        metrics = {"extraction_success_rate": 0.50}
        alerts = self.monitor.evaluate(metrics)
        assert len(alerts) == 1
        assert alerts[0].severity == "critical"

    def test_queue_age_critical(self):
        metrics = {"queue_age_seconds": 700}
        alerts = self.monitor.evaluate(metrics)
        assert len(alerts) == 1
        assert alerts[0].severity == "critical"

    def test_multiple_alerts(self):
        metrics = {
            "extraction_success_rate": 0.50,
            "queue_age_seconds": 700,
            "cache_hit_rate": 0.05,
        }
        alerts = self.monitor.evaluate(metrics)
        assert len(alerts) == 3

    def test_disabled_slo(self):
        """Disabled SLO should not trigger alerts."""
        monitor = SLOMonitor(slos=[
            SLOConfig(name="extraction_success_rate", description="test",
                      warning_threshold=0.99, critical_threshold=0.95, enabled=False),
        ])
        alerts = monitor.evaluate({"extraction_success_rate": 0.0})
        assert len(alerts) == 0


class TestAutoRollback:
    """AutoRollback unit tests."""

    @pytest.mark.asyncio
    async def test_no_observations_no_rollback(self):
        rollback = AutoRollback(min_observations=5)
        result = await rollback.check_and_rollback(None, [])
        assert result is None
