# OverrideStrategy — user-supplied field mappings via `page_fields` in job submission.
# Highest-priority strategy: when a user provides explicit field_name -> selector pairs,
# those mappings take precedence over any domain-specific strategy or overlay.

import hashlib
import json
import logging
import uuid

from bs4 import BeautifulSoup

from src.domain.models import ExtractedRecord, ExtractionSchema

logger = logging.getLogger("Spacescraper.Strategies.Override")


class OverrideStrategy:
    """User-specified field mappings take absolute priority."""

    name: str = "override"

    def _resolve_value(
        self, el, field_name: str, selector: str
    ) -> str | None:
        """Extract a single value from a DOM element using a CSS selector."""
        try:
            found = el.select_one(selector)
            if not found:
                return None
            if found.name in ("img",):
                return found.get("src") or found.get("data-src")
            if found.name in ("a",):
                return found.get("href")
            if found.name in ("meta",):
                return found.get("content")
            return found.get_text(strip=True)
        except Exception:
            return None

    def build_schema(self, mappings: dict[str, str]) -> ExtractionSchema:
        """Build an inline schema from user-specified mappings (one field per entry)."""
        from src.domain.models import FieldDefinition
        fields = []
        for name, selector in mappings.items():
            # Guess type from selector patterns
            if "price" in name.lower() or "rating" in name.lower() or "count" in name.lower():
                ftype = "number"
            elif "url" in name.lower() or "link" in name.lower() or "href" in name.lower():
                ftype = "url"
            else:
                ftype = "string"
            fields.append(FieldDefinition(
                name=name,
                field_type=ftype,
                required=False,
                selector=selector,
            ))
        return ExtractionSchema(
            schema_id=f"inline_override_{hashlib.sha256(json.dumps(mappings, sort_keys=True).encode()).hexdigest()[:16]}",
            record_type="generic",
            fields=fields,
        )

    async def extract(
        self,
        html: str,
        json_payloads: list[dict],
        current_url: str = "",
        overlay: dict | None = None,
        schema: ExtractionSchema | None = None,
    ) -> list[ExtractedRecord]:
        """Run extraction using explicit field_name->selector mappings."""
        if not isinstance(overlay, dict) or not overlay.get("mappings"):
            return []

        mappings: dict[str, str] = overlay["mappings"]
        soup = BeautifulSoup(html, "html.parser")
        records: list[ExtractedRecord] = []

        # Use the user's mapping for every matching container
        container = overlay.get("container_selector")
        containers = soup.select(container) if container else [soup]

        for i, el in enumerate(containers):
            data: dict[str, object] = {}
            for field_name, selector in mappings.items():
                val = self._resolve_value(el, field_name, selector)
                if val is not None:
                    data[field_name] = val
            if not data:
                continue

            built_schema = schema or self.build_schema(mappings)
            record = ExtractedRecord(
                record_id=f"rec_{uuid.uuid4().hex[:12]}",
                record_type="generic",
                data=data,
                source_url=current_url,
                schema_version=built_schema.schema_version,
            )
            # Compute identity hash from data fields
            record.identity_hash = hashlib.sha256(
                json.dumps(data, sort_keys=True, default=str).encode()
            ).hexdigest()
            records.append(record)

        logger.info(
            "OverrideStrategy: extracted %d records from %d containers at %s",
            len(records), len(containers), current_url,
        )
        return records
