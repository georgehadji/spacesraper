# Tests for content-addressed artifact storage.

import os
import json
import pytest
from src.infrastructure.artifact_store import LocalArtifactStore


@pytest.mark.asyncio
async def test_store_and_retrieve():
    store = LocalArtifactStore(base_dir="/tmp/test_artifacts")
    data = b"hello world"
    sha256 = await store.store(data, "https://example.com", "text/html", job_id="job1")
    assert len(sha256) == 64  # SHA256 hex

    retrieved = await store.retrieve(sha256)
    assert retrieved == data

    meta = await store.get_metadata(sha256)
    assert meta is not None
    assert meta.original_url == "https://example.com"
    assert meta.content_type == "text/html"
    assert meta.job_id == "job1"
    assert meta.size_bytes == len(data)

    # Cleanup
    _cleanup(store, sha256)


@pytest.mark.asyncio
async def test_store_idempotent():
    """Storing the same data twice returns same hash and doesn't error."""
    store = LocalArtifactStore(base_dir="/tmp/test_artifacts")
    data = b"duplicate content"
    sha1 = await store.store(data, "https://ex.com", "text/plain")
    sha2 = await store.store(data, "https://ex.com", "text/plain")
    assert sha1 == sha2
    _cleanup(store, sha1)


@pytest.mark.asyncio
async def test_retrieve_nonexistent():
    store = LocalArtifactStore(base_dir="/tmp/test_artifacts")
    result = await store.retrieve("nonexistent" * 8)
    assert result is None


@pytest.mark.asyncio
async def test_get_metadata_nonexistent():
    store = LocalArtifactStore(base_dir="/tmp/test_artifacts")
    meta = await store.get_metadata("0000000000000000000000000000000000000000000000000000000000000000")
    assert meta is None


@pytest.mark.asyncio
async def test_list_by_job():
    store = LocalArtifactStore(base_dir="/tmp/test_artifacts")
    sha1 = await store.store(b"job1 data", "https://a.com", "text/html", job_id="job-list")
    sha2 = await store.store(b"job1 more", "https://b.com", "text/plain", job_id="job-list")
    await store.store(b"other job", "https://c.com", "text/html", job_id="other-job")

    job_artifacts = await store.list_by_job("job-list")
    assert len(job_artifacts) == 2
    assert all(a.job_id == "job-list" for a in job_artifacts)

    _cleanup(store, sha1)
    _cleanup(store, sha2)


@pytest.mark.asyncio
async def test_content_addressed_path():
    """Artifact is stored under artifacts/{xx}/{yy}/{sha256}."""
    store = LocalArtifactStore(base_dir="/tmp/test_artifacts2")
    data = b"path test data"
    sha256 = await store.store(data, "https://ex.com", "text/html")

    expected_dir = os.path.join("/tmp/test_artifacts2", sha256[:2], sha256[2:4])
    expected_file = os.path.join(expected_dir, sha256)
    assert os.path.exists(expected_file)

    # Cleanup
    import shutil
    if os.path.exists("/tmp/test_artifacts2"):
        shutil.rmtree("/tmp/test_artifacts2")


def _cleanup(store: LocalArtifactStore, sha256: str):
    """Remove test artifact files."""
    for path in [store._artifact_path(sha256), store._meta_path(sha256)]:
        if os.path.exists(path):
            os.remove(path)
