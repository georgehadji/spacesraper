# Fuzzy deduplication for extracted records.
# Ported from pipeline.py::DataPipeline._cluster_deduplicates_optimized (W2.2) and
# retargeted from the procurement-specific Opportunity model to the generic
# ExtractedRecord model. Kept as its own collaborator rather than folded into
# the extraction pipeline so the pipeline stays single-responsibility (Section 1).

import logging
from collections import defaultdict
from typing import Any

from thefuzz import fuzz

from src.domain.models import ExtractedRecord

logger = logging.getLogger("Spacescraper.Deduplicator")


class Deduplicator:
    """
    Drops near-duplicate records within a single extraction batch.

    Two-tier matching, same shape as the original Opportunity-specific version:
      1. Exact match on canonical_url (or source_url as fallback) — O(1)
      2. Fuzzy title match within the same record_type group — O(k), k small
    """

    FUZZY_THRESHOLD = 90

    def dedupe(self, records: list[ExtractedRecord]) -> list[ExtractedRecord]:
        if not records:
            return []

        unique: list[ExtractedRecord] = []
        url_index: dict[str, ExtractedRecord] = {}
        type_groups: dict[str, list[ExtractedRecord]] = defaultdict(list)

        for record in records:
            key = record.canonical_url or record.source_url

            if key in url_index:
                continue  # exact duplicate — drop

            if self._is_fuzzy_duplicate(record, type_groups[record.record_type]):
                continue

            unique.append(record)
            url_index[key] = record
            type_groups[record.record_type].append(record)

        return unique

    def _is_fuzzy_duplicate(self, record: ExtractedRecord, candidates: list[ExtractedRecord]) -> bool:
        title = self._title_of(record)
        if not title:
            return False
        for candidate in candidates:
            candidate_title = self._title_of(candidate)
            if candidate_title and fuzz.ratio(title.lower(), candidate_title.lower()) >= self.FUZZY_THRESHOLD:
                return True
        return False

    @staticmethod
    def _title_of(record: ExtractedRecord) -> str | None:
        data: dict[str, Any] = record.data
        return data.get("title") or data.get("name")
