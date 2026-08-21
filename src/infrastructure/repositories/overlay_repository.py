# SQLite adapter for OverlayRepository port.

import json
import logging
from datetime import UTC, datetime

import aiosqlite

from src.domain.models import ExtractionOverlay, ExtractionSchema, OverlayState

logger = logging.getLogger("Spacescraper.OverlayRepository")

CREATE_SCHEMAS_TABLE = """
CREATE TABLE IF NOT EXISTS extraction_schemas (
    schema_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL DEFAULT '1.0',
    record_type TEXT NOT NULL DEFAULT 'generic',
    fields TEXT NOT NULL DEFAULT '[]',
    quality_rules TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

CREATE_OVERLAY_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_overlays_domain ON extraction_overlays(domain)",
    "CREATE INDEX IF NOT EXISTS idx_overlays_state ON extraction_overlays(state)",
]


class SqliteOverlayRepository:
    """SQLite-backed implementation of OverlayRepository."""

    def __init__(self, db_path: str = "spacescraper_jobs.db"):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self):
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute(CREATE_SCHEMAS_TABLE)
        await self._conn.execute(CREATE_OVERLAYS_TABLE)
        # Schema migration: add field_signatures if missing (pre-A4 databases)
        try:
            await self._conn.execute(
                "ALTER TABLE extraction_overlays ADD COLUMN field_signatures TEXT NOT NULL DEFAULT '{}'"
            )
        except Exception:
            pass  # column already exists
        for idx in CREATE_OVERLAY_INDEXES:
            await self._conn.execute(idx)
        await self._conn.commit()
        logger.info("Overlay repository initialized at %s", self.db_path)

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def create_schema(self, schema: ExtractionSchema) -> ExtractionSchema:
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO extraction_schemas
               (schema_id, schema_version, record_type, fields, quality_rules, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                schema.schema_id, schema.schema_version, schema.record_type,
                json.dumps([f.model_dump() for f in schema.fields], default=str),
                json.dumps(schema.quality_rules, default=str),
                schema.created_at.isoformat(),
            ),
        )
        await self._conn.commit()
        return schema

    async def get_schema(self, schema_id: str) -> ExtractionSchema | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM extraction_schemas WHERE schema_id = ?", (schema_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return self._row_to_schema(row) if row else None

    async def list_schemas(self) -> list[ExtractionSchema]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM extraction_schemas ORDER BY created_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_schema(r) for r in rows]

    async def create_overlay(self, overlay: ExtractionOverlay) -> ExtractionOverlay:
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO extraction_overlays
               (overlay_id, domain, schema_id, state, version, container_selector,
                field_mappings, field_signatures, author, source_evidence, rollback_overlay_id,
                validation_result, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                overlay.overlay_id, overlay.domain, overlay.schema_id, overlay.state.value,
                overlay.version, overlay.container_selector,
                json.dumps(overlay.field_mappings, default=str),
                json.dumps(overlay.field_signatures, default=str),
                overlay.author, overlay.source_evidence, overlay.rollback_overlay_id,
                overlay.validation_result,
                overlay.created_at.isoformat(), overlay.updated_at.isoformat(),
            ),
        )
        await self._conn.commit()
        return overlay

    async def get_overlay(self, overlay_id: str) -> ExtractionOverlay | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM extraction_overlays WHERE overlay_id = ?", (overlay_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return self._row_to_overlay(row) if row else None

    async def get_active_overlay(self, domain: str) -> ExtractionOverlay | None:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM extraction_overlays WHERE domain = ? AND state = 'ACTIVE' ORDER BY version DESC LIMIT 1",
            (domain,),
        ) as cursor:
            row = await cursor.fetchone()
            return self._row_to_overlay(row) if row else None

    async def update_overlay_state(
        self, overlay_id: str, new_state: OverlayState,
    ) -> ExtractionOverlay | None:
        assert self._conn is not None
        now = datetime.now(tz=UTC).isoformat()
        await self._conn.execute(
            "UPDATE extraction_overlays SET state = ?, updated_at = ? WHERE overlay_id = ?",
            (new_state.value, now, overlay_id),
        )
        await self._conn.commit()
        return await self.get_overlay(overlay_id)

    async def list_overlays(self, domain: str | None = None) -> list[ExtractionOverlay]:
        assert self._conn is not None
        if domain:
            async with self._conn.execute(
                "SELECT * FROM extraction_overlays WHERE domain = ? ORDER BY version DESC",
                (domain,),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self._conn.execute(
                "SELECT * FROM extraction_overlays ORDER BY domain, version DESC"
            ) as cursor:
                rows = await cursor.fetchall()
        return [self._row_to_overlay(r) for r in rows]

    @staticmethod
    def _row_to_schema(row) -> ExtractionSchema:
        import json as j
        fields_raw = row["fields"]
        if fields_raw.startswith("["):
            fields_data = j.loads(fields_raw)
        else:
            fields_data = []
        return ExtractionSchema(
            schema_id=row["schema_id"],
            schema_version=row["schema_version"],
            record_type=row["record_type"],
            fields=fields_data,
            quality_rules=j.loads(row["quality_rules"]) if row["quality_rules"] else {},
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_overlay(row) -> ExtractionOverlay:
        import json as j
        return ExtractionOverlay(
            overlay_id=row["overlay_id"],
            domain=row["domain"],
            schema_id=row["schema_id"],
            state=OverlayState(row["state"]),
            version=row["version"],
            container_selector=row["container_selector"],
            field_mappings=j.loads(row["field_mappings"]) if row["field_mappings"] else {},
            field_signatures=j.loads(row["field_signatures"]) if "field_signatures" in row.keys() and row["field_signatures"] else {},
            author=row["author"],
            source_evidence=row["source_evidence"],
            rollback_overlay_id=row["rollback_overlay_id"],
            validation_result=row["validation_result"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
