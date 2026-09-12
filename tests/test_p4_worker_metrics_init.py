# P4 D9: three of the four workers record every metric into an uninitialised
# tracker, so SLA alerts can never fire for them.
#
# The audit's fix shape was "initialise in the shared worker entry path rather
# than per worker, so a fourth worker added later inherits it. If no shared
# entry path exists, that absence is the actual finding." There was no shared
# entry path — each worker ended in its own `asyncio.run(worker.run())` — so
# these tests pin the one that was added, and pin that every worker uses it.

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.infrastructure.monitoring.observability import ObservabilityMetrics
from src.infrastructure.worker_runtime import run_worker

REPO_ROOT = Path(__file__).resolve().parents[1]


def _worker_modules() -> list[Path]:
    """Every worker entry point. Globbed, not listed, so a worker added later
    is covered by this test the day it lands."""
    return sorted(REPO_ROOT.glob("worker_*.py"))


def test_there_are_workers_to_check():
    """Guard against the glob silently matching nothing and passing vacuously."""
    assert len(_worker_modules()) >= 4


def test_every_worker_starts_through_the_shared_entry():
    """D9's real fix: one place that initialises telemetry, used by all of them."""
    offenders = []
    for path in _worker_modules():
        source = path.read_text(encoding="utf-8")
        if "from src.infrastructure.worker_runtime import" not in source:
            offenders.append(f"{path.name} (no shared entry import)")
        elif "asyncio.run(" in source:
            offenders.append(f"{path.name} (still starts its own loop)")

    assert not offenders, (
        f"{', '.join(offenders)} — metrics recorded by these workers go to an "
        "uninitialised tracker and are silently discarded"
    )


@pytest.mark.asyncio
async def test_the_shared_entry_initialises_telemetry_before_running():
    """Order matters: a metric recorded before initialize() is dropped."""
    order: list[str] = []
    worker = SimpleNamespace(
        run=AsyncMock(side_effect=lambda: order.append("run")),
    )

    with patch(
        "src.infrastructure.worker_runtime.metrics_tracker.initialize",
        AsyncMock(side_effect=lambda: order.append("initialize")),
    ):
        await run_worker(worker)

    assert order == ["initialize", "run"]


@pytest.mark.asyncio
async def test_an_uninitialised_tracker_silently_drops_what_it_is_told():
    """Why D9 was invisible: increment() returns early with no client, so the
    three workers logged nothing and reported no error."""
    tracker = ObservabilityMetrics(valkey_url="valkey://unused")
    assert tracker._valkey is None

    await tracker.increment("jobs_total", 5)

    assert tracker._local_cache == {}, "an uninitialised tracker appeared to record"


@pytest.mark.asyncio
async def test_the_shared_entry_still_propagates_a_worker_crash():
    """Control: wrapping run() must not swallow the failure that ends a worker."""
    worker = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("worker died")))

    with patch(
        "src.infrastructure.worker_runtime.metrics_tracker.initialize", AsyncMock()
    ), pytest.raises(RuntimeError, match="worker died"):
        await run_worker(worker)
