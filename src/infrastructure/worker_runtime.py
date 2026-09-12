# Shared entry point for every standalone worker process.
#
# D9: metrics_tracker.initialize() was called in main.py and worker_scraper.py
# only. The other three workers recorded into a tracker with no client, and
# ObservabilityMetrics.increment() returns early when there is none — so their
# counters were silently discarded and no SLA alert could ever fire for them.
#
# The absence of any shared worker entry path was the actual finding: four
# workers each ended in their own `asyncio.run(worker.run())`, so anything
# process-wide had to be remembered four times. This is that path. A fifth
# worker gets telemetry by using it, not by remembering to.

import asyncio
import logging
from typing import Protocol

from src.infrastructure.monitoring.observability import metrics_tracker

logger = logging.getLogger("Spacescraper.WorkerRuntime")


class Worker(Protocol):
    """Anything with an async run() loop — every worker_*.py service."""

    async def run(self) -> None: ...


async def run_worker(worker: Worker) -> None:
    """Initialise process-wide telemetry, then hand off to the worker's loop.

    Ordering matters: a metric recorded before initialize() is dropped, so
    this must complete before run() touches metrics_tracker.
    """
    await metrics_tracker.initialize()
    logger.info("Worker runtime: telemetry initialised for %s", type(worker).__name__)
    await worker.run()


def start(worker: Worker) -> None:
    """`if __name__ == "__main__"` entry for a worker module."""
    asyncio.run(run_worker(worker))
