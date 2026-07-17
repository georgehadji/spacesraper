"""Bootstrap module — single composition root for all adapters."""
from src.infrastructure.repositories.job_repository import SqliteJobRepository
from src.infrastructure.repositories.record_repository import SqliteRecordRepository
from src.infrastructure.repositories.outbox_repository import SqliteOutboxRepository
from src.infrastructure.repositories.overlay_repository import SqliteOverlayRepository
from src.infrastructure.repositories.observation_repository import SqliteObservationRepository
from src.infrastructure.queues.stream_queue import RedisStreamQueue
from src.infrastructure.artifact_store import LocalArtifactStore
from src.infrastructure.rate_limiter import DomainRateLimiter
from src.infrastructure.cache import AICache
from src.application.strategy_selector import StrategySelector
from src.application.evaluator import StrategyEvaluator
from src.infrastructure.slo_monitor import SLOMonitor, AutoRollback

# ── Repositories ──
job_repo = SqliteJobRepository()
record_repo = SqliteRecordRepository()
outbox_repo = SqliteOutboxRepository()
overlay_repo = SqliteOverlayRepository()
obs_repo = SqliteObservationRepository()

# ── Infrastructure ──
stream_queue = RedisStreamQueue()
artifact_store = LocalArtifactStore()
rate_limiter = DomainRateLimiter(default_budget=2)
ai_cache = AICache(local_maxsize=500)

# ── Application services (wired with repos) ──
evaluator = StrategyEvaluator(repo=obs_repo)
strategy_selector = StrategySelector(obs_repo=obs_repo)

# ── Monitoring ──
slo_monitor = SLOMonitor()
auto_rollback = AutoRollback()
