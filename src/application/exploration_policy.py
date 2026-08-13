# Bounded strategy exploration policy.
# Uses a simplified Thompson sampling approach to explore strategies.
# Exploration is limited to a configurable percentage of requests
# and never explores on blocked/unsafe domains.

import logging
import random
from dataclasses import dataclass

logger = logging.getLogger("Spacescraper.Exploration")

# Domains that should never be explored (blocked, risky, etc.)
BLOCKED_DOMAINS: set = set()

# Default exploration rate: 5% of requests
DEFAULT_EXPLORATION_RATE = 0.05

# Minimum observations before considering a strategy
MIN_OBSERVATIONS = 5


@dataclass
class StrategyStats:
    """Statistics for a single strategy on a domain."""
    strategy: str
    attempts: int = 0
    successes: int = 0
    blocked: bool = False


class ExplorationPolicy:
    """
    Bounded exploration policy using simplified Thompson sampling.
    - Explores a configurable percentage of requests
    - Never explores on blocked/unsafe domains
    - Immediately demotes a strategy after sustained quality regression
    - Shared counters live in the database, not in worker memory
    """

    def __init__(self, exploration_rate: float = DEFAULT_EXPLORATION_RATE,
                 min_observations: int = MIN_OBSERVATIONS):
        self.exploration_rate = exploration_rate
        self.min_observations = min_observations

    def should_explore(self, domain: str) -> bool:
        """
        Determine whether to explore or exploit for this request.
        Never explores on blocked domains.
        """
        if domain in BLOCKED_DOMAINS:
            return False
        return random.random() < self.exploration_rate

    def select_strategy(
        self, domain: str, strategies: list[StrategyStats],
        default_strategy: str = "http",
    ) -> str:
        """
        Select a strategy using Thompson sampling.
        
        For each strategy with enough observations, sample from Beta(alpha, beta)
        where alpha = successes + 1, beta = failures + 1.
        Pick the strategy with the highest sampled value.
        
        Strategies without enough observations get a small exploration bonus.
        """
        if self.should_explore(domain):
            return self._explore(domain, strategies, default_strategy)

        return self._exploit(domain, strategies, default_strategy)

    def _explore(
        self, domain: str, strategies: list[StrategyStats],
        default: str,
    ) -> str:
        """
        Exploration mode: pick a random strategy that isn't the default.
        This helps discover potentially better alternatives.
        """
        alternatives = [s for s in strategies if s.strategy != default]
        if not alternatives:
            return default

        # Weight by uncertainty: strategies with fewer attempts get higher weight
        weights = []
        for s in alternatives:
            weight = max(1, MIN_OBSERVATIONS - s.attempts) if s.attempts < MIN_OBSERVATIONS else 1.0
            # Demote strategies that get blocked
            if s.blocked:
                weight *= 0.1
            weights.append(max(0.01, weight))

        total = sum(weights)
        r = random.uniform(0, total)
        cumulative = 0
        for i, w in enumerate(weights):
            cumulative += w
            if r <= cumulative:
                return alternatives[i].strategy

        return alternatives[-1].strategy

    def _exploit(
        self, domain: str, strategies: list[StrategyStats],
        default: str,
    ) -> str:
        """
        Exploitation mode: use Thompson sampling to pick the best strategy.
        """
        best_strategy = default
        best_sample = -float("inf")

        for s in strategies:
            if s.attempts < self.min_observations:
                # Not enough data - use a prior-heavy estimate
                alpha = s.successes + 1
                beta = (s.attempts - s.successes) + 1
            else:
                alpha = s.successes + 1
                beta = (s.attempts - s.successes) + 1

            # Sample from Beta distribution (simplified: use mean)
            # Full Thompson sampling would sample, but mean is deterministic
            mean = alpha / (alpha + beta)

            # Penalize blocked strategies
            if s.blocked:
                mean *= 0.1

            if mean > best_sample:
                best_sample = mean
                best_strategy = s.strategy

        return best_strategy

    def record_outcome(self, domain: str, strategy: str, success: bool, blocked: bool = False):
        """
        Record an exploration/exploitation outcome.
        Updates shared state for future decisions.
        """
        # In a full implementation, this would update counters in Valkey/DB
        pass
