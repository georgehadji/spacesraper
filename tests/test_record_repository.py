# Tests for SqliteRecordRepository.

import os
import pytest
from datetime import datetime
from src.infrastructure.repositories.record_repository import SqliteRecordRepository
from src.domain.models import ExtractedRecord, ChangeType


@pytest.mark.asyncio
async def test_create_and_get_record():
    """Record can be created and retrieved."""
    repo = await _make_repo()
    try:
        record = ExtractedRecord(
            record_id="rec-1",
            record_type="product",
            source_url="https://example.com/p1",
            data={"name": "Widget", "price": 9.99},
        )
        created = await repo.create_record(record, job_id="job-1")
        assert created.record_id == "rec-1"

        fetched = await repo.get_record("rec-1")
        assert fetched is not None
        assert fetched.record_type == "product"
        assert fetched.data["name"] == "Widget"
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_list_records_pagination():
    """list_records returns cursor-based pagination."""
    repo = await _make_repo()
    try:
        for i in range(5):
            rec = ExtractedRecord(
                record_id=f"rec-p{i}",
                source_url=f"https://ex.com/{i}",
                data={"idx": i},
            )
            await repo.create_record(rec, job_id="job-page")

        # First page: limit 2
        records, cursor = await repo.list_records("job-page", limit=2)
        assert len(records) == 2
        assert cursor is not None  # more pages exist

        # Second page
        records2, cursor2 = await repo.list_records("job-page", cursor=cursor, limit=2)
        assert len(records2) == 2
        assert cursor2 is not None

        # Third page (last)
        records3, cursor3 = await repo.list_records("job-page", cursor=cursor2, limit=2)
        assert len(records3) == 1
        assert cursor3 is None  # no more pages
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_list_records_empty():
    """list_records on a job with no records returns empty list."""
    repo = await _make_repo()
    try:
        records, cursor = await repo.list_records("nonexistent-job")
        assert records == []
        assert cursor is None
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_get_record_not_found():
    """get_record returns None for non-existent record."""
    repo = await _make_repo()
    try:
        result = await repo.get_record("does-not-exist")
        assert result is None
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_update_record_data():
    """update_record can modify data field."""
    repo = await _make_repo()
    try:
        rec = ExtractedRecord(
            record_id="rec-update",
            source_url="https://ex.com/u",
            data={"name": "Old"},
        )
        await repo.create_record(rec, job_id="job-u")

        updated = await repo.update_record("rec-update", data={"name": "New", "price": 5.0})
        assert updated is not None
        assert updated.data["name"] == "New"
        assert updated.data["price"] == 5.0
    finally:
        await _cleanup(repo)


@pytest.mark.asyncio
async def test_record_count():
    """get_record_count returns the correct count."""
    repo = await _make_repo()
    try:
        for i in range(3):
            rec = ExtractedRecord(
                record_id=f"rec-c{i}",
                source_url=f"https://ex.com/{i}",
            )
            await repo.create_record(rec, job_id="job-count")

        count = await repo.get_record_count("job-count")
        assert count == 3

        # Other job has 0
        count2 = await repo.get_record_count("other-job")
        assert count2 == 0
    finally:
        await _cleanup(repo)


async def _make_repo() -> SqliteRecordRepository:
    repo = SqliteRecordRepository(db_path="test_records.db")
    await repo.initialize()
    return repo


async def _cleanup(repo: SqliteRecordRepository):
    await repo.close()
    for suffix in ("", "-wal", "-shm"):
        path = f"test_records.db{suffix}"
        if os.path.exists(path):
            os.remove(path)
