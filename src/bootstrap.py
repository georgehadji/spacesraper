# Bootstrap — single composition root, built once per process and shared by
# main.py (API) and both worker entrypoints (worker_scraper.py,
# worker_processor.py). P1-P6 (docs/plans/2026-08-13-capability-enhancement-plan.md)
# each add constructor-injected dependencies; this is the one place that wires
# them, instead of three separate ad-hoc construction sites.

import os
from dataclasses import dataclass

from src.application.reaper import JobReaper
from src.application.strategy_selector import StrategySelector
from src.infrastructure.outbox_relay import OutboxRelay
from src.infrastructure.queues.stream_queue import ValkeyStreamQueue
from src.infrastructure.repositories.job_repository import SqliteJobRepository
from src.infrastructure.repositories.observation_repository import SqliteObservationRepository
from src.infrastructure.repositories.outbox_repository import SqliteOutboxRepository
from src.infrastructure.repositories.overlay_repository import SqliteOverlayRepository
from src.infrastructure.repositories.record_repository import SqliteRecordRepository

VALKEY_URL = os.environ.get("VALKEY_URL", "valkey://localhost:6379")


@dataclass
class AppContainer:
    """Every repository, the message bus, and the background services built
    from them — constructed once per process and handed out from here rather
    than as bare module globals or per-file ad-hoc construction (W4.1/W4.2)."""

    stream_queue: ValkeyStreamQueue
    job_repo: SqliteJobRepository
    record_repo: SqliteRecordRepository
    outbox_repo: SqliteOutboxRepository
    overlay_repo: SqliteOverlayRepository
    obs_repo: SqliteObservationRepository
    strategy_selector: StrategySelector
    outbox_relay: OutboxRelay
    job_reaper: JobReaper

    @classmethod
    def build(cls, valkey_url: str) -> "AppContainer":
        stream_queue = ValkeyStreamQueue(valkey_url=valkey_url)
        obs_repo = SqliteObservationRepository()
        outbox_repo = SqliteOutboxRepository()
        job_repo = SqliteJobRepository()
        return cls(
            stream_queue=stream_queue,
            job_repo=job_repo,
            record_repo=SqliteRecordRepository(),
            outbox_repo=outbox_repo,
            overlay_repo=SqliteOverlayRepository(),
            obs_repo=obs_repo,
            strategy_selector=StrategySelector(obs_repo),
            outbox_relay=OutboxRelay(outbox_repo, stream_queue=stream_queue),
            job_reaper=JobReaper(job_repo),
        )

    def repos(self):
        """The five repos with an initialize()/close() lifecycle, for lifespan."""
        return (self.job_repo, self.record_repo, self.outbox_repo, self.obs_repo, self.overlay_repo)


container = AppContainer.build(VALKEY_URL)
