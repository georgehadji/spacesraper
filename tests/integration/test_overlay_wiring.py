# Integration test for W3.1/W3.2 (C1): promoting an overlay to ACTIVE must have
# a real, observable effect on the live extraction path. Before this workstream,
# worker_processor.py wired DeterministicExtractionPipeline(overlay_repo=None),
# so OverlayRepository.get_active_overlay() was never consulted — promotion was
# a no-op against the live extractor.

import os

import pytest

from src.application.extraction_pipeline import DeterministicExtractionPipeline
from src.domain.models import ExtractionOverlay, OverlayState
from src.infrastructure.repositories.overlay_repository import SqliteOverlayRepository

DB_PATH = "test_overlay_wiring.db"


async def _make_repo() -> SqliteOverlayRepository:
    repo = SqliteOverlayRepository(db_path=DB_PATH)
    await repo.initialize()
    return repo


async def _cleanup(repo: SqliteOverlayRepository):
    await repo.close()
    for suffix in ("", "-wal", "-shm"):
        path = f"{DB_PATH}{suffix}"
        if os.path.exists(path):
            os.remove(path)


@pytest.mark.asyncio
async def test_active_overlay_is_used_by_live_extraction_path():
    """
    Promote an overlay to ACTIVE for a domain, then run the surviving
    extraction strategy against that domain with no inline overlay dict —
    exactly how worker_processor.py calls it. The repository-backed overlay
    must be picked up automatically.
    """
    repo = await _make_repo()
    try:
        overlay = ExtractionOverlay(
            overlay_id="ov-1",
            domain="example.com",
            schema_id="s1",
            state=OverlayState.CANDIDATE,
            container_selector=".item",
            field_mappings={"title": "h3", "price": ".price"},
        )
        await repo.create_overlay(overlay)
        await repo.update_overlay_state("ov-1", OverlayState.ACTIVE)

        # Same wiring worker_processor.py uses: a live repo, no inline overlay.
        pipeline = DeterministicExtractionPipeline(overlay_repo=repo)
        html = '<div class="item"><h3>Widget</h3><span class="price">£9.99</span></div>'

        results = await pipeline.extract(html, [], current_url="https://example.com/list")

        assert len(results) == 1
        assert results[0].data["title"] == "Widget"
        assert results[0].data["price"] == "£9.99"
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_non_active_overlay_is_not_used():
    """A CANDIDATE (not yet promoted) overlay must not affect live extraction."""
    repo = await _make_repo()
    try:
        overlay = ExtractionOverlay(
            overlay_id="ov-2",
            domain="pending.com",
            schema_id="s1",
            state=OverlayState.CANDIDATE,
            container_selector=".item",
            field_mappings={"title": "h3"},
        )
        await repo.create_overlay(overlay)
        # Never promoted — stays CANDIDATE.

        pipeline = DeterministicExtractionPipeline(overlay_repo=repo)
        html = '<div class="item"><h3>Widget</h3></div>'

        results = await pipeline.extract(html, [], current_url="https://pending.com/list")

        # Falls through to JSON-LD / semantic-HTML stages, neither of which
        # matches this fixture, so the CANDIDATE overlay must not fire.
        assert results == []
    finally:
        await _cleanup(repo)
