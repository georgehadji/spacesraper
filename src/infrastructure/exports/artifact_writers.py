# Generic artifact writers for extracted records.
# Produces CSV and JSON files from ExtractedRecord data.
# Replaces the Opportunity-specific ReportGenerator for new code.

import csv
import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Protocol

from src.domain.models import ExtractedRecord

logger = logging.getLogger("Spacescraper.ArtifactWriter")


class ArtifactWriter(ABC):
    """Base protocol for writing extracted records to files."""

    @abstractmethod
    async def write(self, records: list[ExtractedRecord], target_dir: str, name_prefix: str) -> list[str]:
        """
        Write records to artifact files.
        Returns list of file paths created.
        """
        ...


class CsvArtifactWriter(ArtifactWriter):
    """Writes ExtractedRecord data to CSV files."""

    def __init__(self, flatten: bool = True):
        self.flatten = flatten

    async def write(self, records: list[ExtractedRecord], target_dir: str, name_prefix: str) -> list[str]:
        if not records:
            return []

        os.makedirs(target_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = os.path.join(target_dir, f"{name_prefix}_records_{timestamp}.csv")
        written = []

        try:
            fieldnames = [
                "record_id", "record_type", "schema_version",
                "canonical_url", "source_url", "first_seen", "last_seen",
                "change_type",
            ]

            with open(filepath, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for record in records:
                    row = record.model_dump(mode="json")
                    # Flatten top-level fields; data dict is skipped in extrasaction=ignore
                    writer.writerow(row)

            logger.info("CsvArtifactWriter: Wrote %d records to %s", len(records), filepath)
            written.append(filepath)
        except Exception as e:
            logger.error("CsvArtifactWriter: Failed to write %s: %s", filepath, e)

        return written


class JsonArtifactWriter(ArtifactWriter):
    """Writes ExtractedRecord data to JSON files."""

    async def write(self, records: list[ExtractedRecord], target_dir: str, name_prefix: str) -> list[str]:
        if not records:
            return []

        os.makedirs(target_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = os.path.join(target_dir, f"{name_prefix}_records_{timestamp}.json")
        written = []

        try:
            data = [record.model_dump(mode="json") for record in records]
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            logger.info("JsonArtifactWriter: Wrote %d records to %s", len(records), filepath)
            written.append(filepath)
        except Exception as e:
            logger.error("JsonArtifactWriter: Failed to write %s: %s", filepath, e)

        return written


async def write_artifacts(
    records: list[ExtractedRecord],
    target_dir: str = "exports",
    name_prefix: str = "extraction",
    formats: list[str] = None,
) -> list[str]:
    """
    Convenience function to write records in multiple formats.
    Returns list of all written file paths.
    """
    if formats is None:
        formats = ["csv", "json"]

    writers = {
        "csv": CsvArtifactWriter(),
        "json": JsonArtifactWriter(),
    }

    written = []
    for fmt in formats:
        writer = writers.get(fmt)
        if writer:
            paths = await writer.write(records, target_dir, name_prefix)
            written.extend(paths)

    return written
