# Bootstrap — single composition root, built once per process and shared by
# main.py (API) and both worker entrypoints (worker_scraper.py,
# worker_processor.py). P1-P6 (docs/plans/2026-08-13-capability-enhancement-plan.md)
# each add constructor-injected dependencies; this is the one place that wires
# them, instead of three separate ad-hoc construction sites.

import os
from dataclasses import dataclass

from src.application.reaper import JobReaper
from src.application.strategy_selector import StrategySelector
from src.domain.ports import (
    JobRepository,
    ObservationRepository,
    OutboxRepository,
    OverlayRepository,
    RecordRepository,
)
from src.infrastructure.outbox_relay import OutboxRelay
from src.infrastructure.queues.stream_queue import ValkeyStreamQueue
from src.infrastructure.repositories.factory import (
    make_job_repository,
    make_observation_repository,
    make_outbox_repository,
    make_overlay_repository,
    make_record_repository,
)

VALKEY_URL = os.environ.get("VALKEY_URL", "valkey://localhost:6379")


@dataclass
class AppContainer:
    """Every repository, the message bus, and the background services built
    from them — constructed once per process and handed out from here rather
    than as bare module globals or per-file ad-hoc construction (W4.1/W4.2).

    Repo fields are typed against the domain ports (Protocols), not the
    concrete Sqlite* classes — factory.py picks SQLite or Postgres per
    PERSISTENCE_BACKEND (C8/W5.3) and both satisfy the same contract."""

    stream_queue: ValkeyStreamQueue
    job_repo: JobRepository
    record_repo: RecordRepository
    outbox_repo: OutboxRepository
    overlay_repo: OverlayRepository
    obs_repo: ObservationRepository
    strategy_selector: StrategySelector
    outbox_relay: OutboxRelay
    job_reaper: JobReaper

    @classmethod
    def build(cls, valkey_url: str) -> "AppContainer":
        stream_queue = ValkeyStreamQueue(valkey_url=valkey_url)
        obs_repo = make_observation_repository()
        outbox_repo = make_outbox_repository()
        job_repo = make_job_repository()
        return cls(
            stream_queue=stream_queue,
            job_repo=job_repo,
            record_repo=make_record_repository(),
            outbox_repo=outbox_repo,
            overlay_repo=make_overlay_repository(),
            obs_repo=obs_repo,
            strategy_selector=StrategySelector(obs_repo),
            outbox_relay=OutboxRelay(outbox_repo, stream_queue=stream_queue),
            job_reaper=JobReaper(job_repo),
        )

    def repos(self):
        """The five repos with an initialize()/close() lifecycle, for lifespan."""
        return (self.job_repo, self.record_repo, self.outbox_repo, self.obs_repo, self.overlay_repo)


container = AppContainer.build(VALKEY_URL)
