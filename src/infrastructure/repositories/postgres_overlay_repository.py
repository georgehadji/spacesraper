# Postgres adapter for OverlayRepository port (C8/W5.3).
# Mirrors SqliteOverlayRepository — see postgres_job_repository.py's module
# docstring for the single-connection design rationale.

import json
import logging
from datetime import UTC, datetime
from typing import Any

import asyncpg

from src.config_settings import settings
from src.domain.models import ExtractionOverlay, ExtractionSchema, OverlayState
from src.infrastructure.repositories.postgres_conn import PostgresConnection, asyncpg_dsn, create_pool_with_retry

logger = logging.getLogger("Spacescraper.PostgresOverlayRepository")

CREATE_SCHEMAS_TABLE = """
CREATE TABLE IF NOT EXISTS extraction_schemas (
    schema_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL DEFAULT '1.0',
    record_type TEXT NOT NULL DEFAULT 'generic',
    fields TEXT NOT NULL DEFAULT '[]',
    quality_rules TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL
)
"""

CREATE_OVERLAYS_TABLE = """
CREATE TABLE IF NOT EXISTS extraction_overlays (
    overlay_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    schema_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'CANDIDATE',
    version INTEGER NOT NULL DEFAULT 1,
    container_selector TEXT,
    field_mappings TEXT NOT NULL DEFAULT '{}',
    field_signatures TEXT NOT NULL DEFAULT '{}',
    author TEXT,
    source_evidence TEXT,
    rollback_overlay_id TEXT,
    validation_result TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
)
"""

CREATE_OVERLAY_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_overlays_domain ON extraction_overlays(domain)",
    "CREATE INDEX IF NOT EXISTS idx_overlays_state ON extraction_overlays(state)",
]


class PostgresOverlayRepository:
    """Postgres-backed implementation of OverlayRepository. Mirrors SqliteOverlayRepository."""

    def __init__(self, dsn: str):
        self.dsn = asyncpg_dsn(dsn)
        self._pool: asyncpg.Pool | None = None
        self._conn: PostgresConnection | None = None

    async def initialize(self) -> None:
        self._pool = await create_pool_with_retry(
            self.dsn, min_size=2, max_size=settings.database.pool_size + settings.database.max_overflow,
        )
        self._conn = PostgresConnection(self._pool)
        await self._conn.execute(CREATE_SCHEMAS_TABLE)
        await self._conn.execute(CREATE_OVERLAYS_TABLE)
        for idx in CREATE_OVERLAY_INDEXES:
            await self._conn.execute(idx)
        logger.info("Overlay repository initialized at Postgres")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            self._conn = None

    async def create_schema(self, schema: ExtractionSchema) -> ExtractionSchema:
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO extraction_schemas
               (schema_id, schema_version, record_type, fields, quality_rules, created_at)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            schema.schema_id, schema.schema_version, schema.record_type,
            json.dumps([f.model_dump() for f in schema.fields], default=str),
            json.dumps(schema.quality_rules, default=str),
            schema.created_at,
        )
        return schema

    async def get_schema(self, schema_id: str) -> ExtractionSchema | None:
        assert self._conn is not None
        row = await self._conn.fetchrow("SELECT * FROM extraction_schemas WHERE schema_id = $1", schema_id)
        return self._row_to_schema(row) if row else None

    async def list_schemas(self, limit: int = 50, offset: int = 0) -> list[ExtractionSchema]:
        assert self._conn is not None
        rows = await self._conn.fetch(
            "SELECT * FROM extraction_schemas ORDER BY created_at DESC LIMIT $1 OFFSET $2",
            limit, offset,
        )
        return [self._row_to_schema(r) for r in rows]

    async def create_overlay(self, overlay: ExtractionOverlay) -> ExtractionOverlay:
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO extraction_overlays
               (overlay_id, domain, schema_id, state, version, container_selector,
                field_mappings, field_signatures, author, source_evidence, rollback_overlay_id,
                validation_result, created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)""",
            overlay.overlay_id, overlay.domain, overlay.schema_id, overlay.state.value,
            overlay.version, overlay.container_selector,
            json.dumps(overlay.field_mappings, default=str),
            json.dumps(overlay.field_signatures, default=str),
            overlay.author, overlay.source_evidence, overlay.rollback_overlay_id,
            overlay.validation_result,
            overlay.created_at, overlay.updated_at,
        )
        return overlay

    async def get_overlay(self, overlay_id: str) -> ExtractionOverlay | None:
        assert self._conn is not None
        row = await self._conn.fetchrow("SELECT * FROM extraction_overlays WHERE overlay_id = $1", overlay_id)
        return self._row_to_overlay(row) if row else None

    async def get_active_overlay(self, domain: str) -> ExtractionOverlay | None:
        assert self._conn is not None
        row = await self._conn.fetchrow(
            "SELECT * FROM extraction_overlays WHERE domain = $1 AND state = 'ACTIVE' ORDER BY version DESC LIMIT 1",
            domain,
        )
        return self._row_to_overlay(row) if row else None

    async def update_overlay_state(
        self, overlay_id: str, new_state: OverlayState,
    ) -> ExtractionOverlay | None:
        assert self._conn is not None
        now = datetime.now(tz=UTC)
        await self._conn.execute(
            "UPDATE extraction_overlays SET state = $1, updated_at = $2 WHERE overlay_id = $3",
            new_state.value, now, overlay_id,
        )
        return await self.get_overlay(overlay_id)

    async def list_overlays(
        self, domain: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[ExtractionOverlay]:
        assert self._conn is not None
        if domain:
            rows = await self._conn.fetch(
                "SELECT * FROM extraction_overlays WHERE domain = $1 ORDER BY version DESC LIMIT $2 OFFSET $3",
                domain, limit, offset,
            )
        else:
            rows = await self._conn.fetch(
                "SELECT * FROM extraction_overlays ORDER BY domain, version DESC LIMIT $1 OFFSET $2",
                limit, offset,
            )
        return [self._row_to_overlay(r) for r in rows]

    @staticmethod
    def _row_to_schema(row: Any) -> ExtractionSchema:
        fields_raw = row["fields"]
        fields_data = json.loads(fields_raw) if fields_raw.startswith("[") else []
        return ExtractionSchema(
            schema_id=row["schema_id"],
            schema_version=row["schema_version"],
            record_type=row["record_type"],
            fields=fields_data,
            quality_rules=json.loads(row["quality_rules"]) if row["quality_rules"] else {},
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_overlay(row: Any) -> ExtractionOverlay:
        return ExtractionOverlay(
            overlay_id=row["overlay_id"],
            domain=row["domain"],
            schema_id=row["schema_id"],
            state=OverlayState(row["state"]),
            version=row["version"],
            container_selector=row["container_selector"],
            field_mappings=json.loads(row["field_mappings"]) if row["field_mappings"] else {},
            field_signatures=json.loads(row["field_signatures"]) if row["field_signatures"] else {},
            author=row["author"],
            source_evidence=row["source_evidence"],
            rollback_overlay_id=row["rollback_overlay_id"],
            validation_result=row["validation_result"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
