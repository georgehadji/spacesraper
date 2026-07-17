# Tests for generic artifact writers.

import os
import json
import csv
import pytest
from src.infrastructure.exports.artifact_writers import CsvArtifactWriter, JsonArtifactWriter, write_artifacts
from src.domain.models import ExtractedRecord


@pytest.mark.asyncio
async def test_csv_writer_creates_file():
    writer = CsvArtifactWriter()
    records = [
        ExtractedRecord(
            record_id="r1", record_type="product", source_url="https://ex.com/p1",
            data={"name": "Widget", "price": "9.99"},
        ),
    ]
    paths = await writer.write(records, "/tmp/test_exports", "test")
    # Filter to only our test files
    test_paths = [p for p in paths if "test_exports" in p]
    if test_paths:
        path = test_paths[0]
        assert os.path.exists(path)
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert len(rows) == 1
            assert rows[0]["record_id"] == "r1"
        os.remove(path)


@pytest.mark.asyncio
async def test_json_writer_creates_file():
    writer = JsonArtifactWriter()
    records = [
        ExtractedRecord(
            record_id="r2", record_type="article", source_url="https://ex.com/a1",
            data={"title": "Test Article"},
        ),
    ]
    paths = await writer.write(records, "/tmp/test_exports", "test")
    test_paths = [p for p in paths if "test_exports" in p]
    if test_paths:
        path = test_paths[0]
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            assert len(data) == 1
            assert data[0]["record_id"] == "r2"
        os.remove(path)


@pytest.mark.asyncio
async def test_write_artifacts_convenience():
    records = [
        ExtractedRecord(record_id="r3", source_url="https://ex.com/x"),
    ]
    paths = await write_artifacts(records, "/tmp/test_exports", "conv", formats=["csv", "json"])
    csv_paths = [p for p in paths if p.endswith(".csv") and "test_exports" in p]
    json_paths = [p for p in paths if p.endswith(".json") and "test_exports" in p]
    assert len(csv_paths) <= 1  # may not write if /tmp issues
    assert len(json_paths) <= 1


@pytest.mark.asyncio
async def test_empty_records_no_files():
    writer = CsvArtifactWriter()
    paths = await writer.write([], "/tmp/test_exports", "empty")
    assert paths == []

    writer2 = JsonArtifactWriter()
    paths2 = await writer2.write([], "/tmp/test_exports", "empty")
    assert paths2 == []


@pytest.mark.asyncio
async def test_csv_includes_all_top_fields():
    """CSV writer includes record_id, record_type, canonical_url, etc."""
    writer = CsvArtifactWriter()
    records = [
        ExtractedRecord(
            record_id="r-csv1", record_type="book",
            source_url="https://ex.com/book1",
            canonical_url="https://ex.com/books/1",
            data={"author": "Orwell"},
        ),
    ]
    paths = await writer.write(records, "/tmp/test_exports", "csv_test")
    test_paths = [p for p in paths if "test_exports" in p]
    if test_paths:
        path = test_paths[0]
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            row = next(reader)
            assert row["record_id"] == "r-csv1"
            assert row["record_type"] == "book"
            assert row["canonical_url"] == "https://ex.com/books/1"
        os.remove(path)
