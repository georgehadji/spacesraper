# Tests for evaluator, shadow evaluator, strategy selector, SLO monitor, exploration policy.
# Run via: python -m pytest tests/test_increment_modules.py -v

import pytest
from datetime import datetime, timedelta
from src.domain.models import StrategyObservation, EvaluationResult, DomainProfile
from src.application.evaluator import StrategyEvaluator
from src.application.exploration_policy import ExplorationPolicy, StrategyStats
from src.infrastructure.slo_monitor import SLOMonitor, AutoRollback, SLOConfig


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

    def test_block_rate_high_is_critical(self):
        """block_rate is higher-is-worse: a low block rate must never alert,
        and a high one must (regression test for the fixed threshold direction)."""
        assert len(self.monitor.evaluate({"block_rate": 0.02})) == 0
        alerts = self.monitor.evaluate({"block_rate": 0.30})
        assert len(alerts) == 1
        assert alerts[0].severity == "critical"

    def test_llm_groundedness_warning_and_critical(self):
        """Task 5.3: llm_groundedness SLO — lower groundedness is worse."""
        assert len(self.monitor.evaluate({"llm_groundedness": 0.95})) == 0

        warn_alerts = self.monitor.evaluate({"llm_groundedness": 0.6})
        assert len(warn_alerts) == 1
        assert warn_alerts[0].severity == "warning"

        crit_alerts = self.monitor.evaluate({"llm_groundedness": 0.3})
        assert len(crit_alerts) == 1
        assert crit_alerts[0].severity == "critical"

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


class TestExplorationPolicy:
    """ExplorationPolicy unit tests."""

    def test_explore_selects_alternative(self):
        policy = ExplorationPolicy(exploration_rate=1.0)  # Always explore
        strategies = [
            StrategyStats(strategy="http", attempts=10, successes=8),
            StrategyStats(strategy="browser", attempts=2, successes=1),
        ]
        result = policy._explore("test.com", strategies, "http")
        assert result == "browser"  # Should pick the alternative

    def test_exploit_prefers_best(self):
        policy = ExplorationPolicy(exploration_rate=0.0)  # Always exploit
        strategies = [
            StrategyStats(strategy="http", attempts=10, successes=9),
            StrategyStats(strategy="browser", attempts=10, successes=1),
        ]
        result = policy._exploit("test.com", strategies, "http")
        assert result == "http"  # Higher success rate

    def test_blocked_domain_no_explore(self):
        policy = ExplorationPolicy(exploration_rate=1.0)
        from src.application.exploration_policy import BLOCKED_DOMAINS
        BLOCKED_DOMAINS.add("evil.com")
        assert not policy.should_explore("evil.com")
        BLOCKED_DOMAINS.discard("evil.com")

    def test_normal_domain_may_explore(self):
        policy = ExplorationPolicy(exploration_rate=1.0)
        assert policy.should_explore("normal.com")

    def test_exploit_with_blocked_strategy(self):
        """Blocked strategies get penalized during exploitation."""
        policy = ExplorationPolicy(exploration_rate=0.0)
        strategies = [
            StrategyStats(strategy="http", attempts=10, successes=5),
            StrategyStats(strategy="browser", attempts=10, successes=8, blocked=True),
        ]
        # browser has higher success rate but is blocked, http should win
        result = policy._exploit("test.com", strategies, "http")
        assert result == "http"
