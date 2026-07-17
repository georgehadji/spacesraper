# Tests for ExtractionSchema, ExtractionOverlay, and SqliteOverlayRepository.

import os
import pytest
from src.domain.models import ExtractionSchema, FieldDefinition, ExtractionOverlay, OverlayState
from src.infrastructure.repositories.overlay_repository import SqliteOverlayRepository


class TestSchemaValidation:
    """ExtractionSchema.validate_record unit tests."""

    def test_valid_record_passes(self):
        schema = ExtractionSchema(
            schema_id="s1",
            fields=[
                FieldDefinition(name="title", field_type="string", required=True),
                FieldDefinition(name="price", field_type="number"),
            ],
        )
        errors = schema.validate_record({"title": "Widget", "price": "9.99"})
        assert errors == []

    def test_missing_required_field(self):
        schema = ExtractionSchema(
            schema_id="s2",
            fields=[FieldDefinition(name="title", field_type="string", required=True)],
        )
        errors = schema.validate_record({"price": "5"})
        assert len(errors) == 1
        assert "Missing required field: title" in errors[0]

    def test_invalid_number(self):
        schema = ExtractionSchema(
            schema_id="s3",
            fields=[FieldDefinition(name="price", field_type="number")],
        )
        errors = schema.validate_record({"price": "not_a_number"})
        assert len(errors) == 1
        assert "should be numeric" in errors[0]

    def test_valid_number_with_currency(self):
        schema = ExtractionSchema(
            schema_id="s4",
            fields=[FieldDefinition(name="price", field_type="number")],
        )
        errors = schema.validate_record({"price": "€299.00"})
        assert errors == []

    def test_url_validation(self):
        schema = ExtractionSchema(
            schema_id="s5",
            fields=[FieldDefinition(name="link", field_type="url")],
        )
        errors = schema.validate_record({"link": "not-a-url"})
        assert len(errors) == 1
        assert "should be a URL" in errors[0]

        errors = schema.validate_record({"link": "https://example.com"})
        assert errors == []


@pytest.mark.asyncio
async def test_create_and_get_schema():
    repo = await _make_repo()
    try:
        schema = ExtractionSchema(
            schema_id="test-schema",
            fields=[FieldDefinition(name="name", field_type="string", required=True)],
        )
        await repo.create_schema(schema)
        fetched = await repo.get_schema("test-schema")
        assert fetched is not None
        assert fetched.schema_id == "test-schema"
        assert len(fetched.fields) == 1
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_create_and_get_overlay():
    repo = await _make_repo()
    try:
        overlay = ExtractionOverlay(
            overlay_id="ov-test",
            domain="example.com",
            schema_id="s1",
            field_mappings={"title": "h1.title"},
            state=OverlayState.CANDIDATE,
        )
        await repo.create_overlay(overlay)
        fetched = await repo.get_overlay("ov-test")
        assert fetched is not None
        assert fetched.domain == "example.com"
        assert fetched.state == OverlayState.CANDIDATE
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_get_active_overlay():
    repo = await _make_repo()
    try:
        candidate = ExtractionOverlay(
            overlay_id="ov-c1", domain="test.com", schema_id="s1",
            state=OverlayState.CANDIDATE, version=1,
        )
        active = ExtractionOverlay(
            overlay_id="ov-a1", domain="test.com", schema_id="s1",
            state=OverlayState.ACTIVE, version=2,
        )
        await repo.create_overlay(candidate)
        await repo.create_overlay(active)

        fetched = await repo.get_active_overlay("test.com")
        assert fetched is not None
        assert fetched.overlay_id == "ov-a1"
        assert fetched.state == OverlayState.ACTIVE
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_update_overlay_state():
    repo = await _make_repo()
    try:
        overlay = ExtractionOverlay(
            overlay_id="ov-upd", domain="x.com", schema_id="s1",
            state=OverlayState.CANDIDATE, version=1,
        )
        await repo.create_overlay(overlay)

        updated = await repo.update_overlay_state("ov-upd", OverlayState.SHADOW)
        assert updated.state == OverlayState.SHADOW

        # Idempotent
        updated2 = await repo.update_overlay_state("ov-upd", OverlayState.SHADOW)
        assert updated2.state == OverlayState.SHADOW
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_list_overlays():
    repo = await _make_repo()
    try:
        for i in range(3):
            o = ExtractionOverlay(
                overlay_id=f"ov-list-{i}", domain=f"site{i}.com",
                schema_id="s1", version=i,
            )
            await repo.create_overlay(o)

        all_overlays = await repo.list_overlays()
        assert len(all_overlays) == 3

        domain_overlays = await repo.list_overlays(domain="site0.com")
        assert len(domain_overlays) == 1
    finally:
        await _cleanup(repo)


async def _make_repo() -> SqliteOverlayRepository:
    repo = SqliteOverlayRepository(db_path="test_overlay.db")
    await repo.initialize()
    return repo


async def _cleanup(repo: SqliteOverlayRepository):
    await repo.close()
    for suffix in ("", "-wal", "-shm"):
        path = f"test_overlay.db{suffix}"
        if os.path.exists(path):
            os.remove(path)
