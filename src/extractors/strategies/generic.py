# Generic fallback strategy — used when no domain-specific strategy matches.
# Delegates to the legacy pipeline (overlay → JSON-LD → semantic HTML).

import logging

from src.domain.models import ExtractedRecord, ExtractionSchema

logger = logging.getLogger("Spacescraper.Strategies.Generic")


class GenericStrategy:
    """Fallback: delegates to the default 3-stage pipeline."""

    name: str = "generic"

    async def extract(
        self,
        html: str,
        json_payloads: list[dict],
        current_url: str = "",
        overlay: dict | None = None,
        schema: ExtractionSchema | None = None,
    ) -> list[ExtractedRecord]:
        """Run generic JSON-LD + semantic HTML extraction."""
        from src.application.extraction_pipeline import DeterministicExtractionPipeline
        pipeline = DeterministicExtractionPipeline()
        return await pipeline.extract(html, json_payloads, current_url, overlay, schema)
