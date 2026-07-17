# Content-addressed artifact storage.
# Stores raw data under artifacts/{sha256[:2]}/{sha256[2:4]}/{sha256}
# with metadata tracking.

import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Optional, List, Protocol
from abc import ABC, abstractmethod

logger = logging.getLogger("Spacescraper.ArtifactStore")

ARTIFACT_DIR = "artifacts"


class ArtifactMetadata:
    """Metadata stored alongside each artifact."""
    def __init__(self, sha256: str, original_url: str, content_type: str,
                 size_bytes: int, created_at: str, job_id: str = ""):
        self.sha256 = sha256
        self.original_url = original_url
        self.content_type = content_type
        self.size_bytes = size_bytes
        self.created_at = created_at
        self.job_id = job_id

    def to_dict(self) -> dict:
        return {
            "sha256": self.sha256,
            "original_url": self.original_url,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "job_id": self.job_id,
        }

    @staticmethod
    def from_dict(d: dict) -> "ArtifactMetadata":
        return ArtifactMetadata(
            sha256=d["sha256"],
            original_url=d.get("original_url", ""),
            content_type=d.get("content_type", ""),
            size_bytes=d.get("size_bytes", 0),
            created_at=d.get("created_at", ""),
            job_id=d.get("job_id", ""),
        )


class ArtifactStore(ABC):
    """Port for storing and retrieving raw artifacts."""

    @abstractmethod
    async def store(self, data: bytes, original_url: str, content_type: str,
                    job_id: str = "") -> str:
        """
        Store data and return its content-addressed path (SHA256).
        Stores artifacts under artifacts/{xx}/{yy}/{sha256}.
        """
        ...

    @abstractmethod
    async def retrieve(self, sha256: str) -> Optional[bytes]:
        """Retrieve artifact data by its SHA256 hash."""
        ...

    @abstractmethod
    async def get_metadata(self, sha256: str) -> Optional[ArtifactMetadata]:
        """Get metadata for a stored artifact."""
        ...

    @abstractmethod
    async def list_by_job(self, job_id: str) -> List[ArtifactMetadata]:
        """List all artifacts for a given job."""
        ...


class LocalArtifactStore(ArtifactStore):
    """Filesystem-based content-addressed artifact store."""

    def __init__(self, base_dir: str = ARTIFACT_DIR):
        self.base_dir = base_dir

    def _artifact_path(self, sha256: str) -> str:
        return os.path.join(self.base_dir, sha256[:2], sha256[2:4], sha256)

    def _meta_path(self, sha256: str) -> str:
        return self._artifact_path(sha256) + ".meta.json"

    async def store(self, data: bytes, original_url: str, content_type: str,
                    job_id: str = "") -> str:
        sha256 = hashlib.sha256(data).hexdigest()
        filepath = self._artifact_path(sha256)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        # Write data file
        with open(filepath, "wb") as f:
            f.write(data)

        # Write metadata
        meta = ArtifactMetadata(
            sha256=sha256,
            original_url=original_url,
            content_type=content_type,
            size_bytes=len(data),
            created_at=datetime.utcnow().isoformat(),
            job_id=job_id,
        )
        with open(self._meta_path(sha256), "w", encoding="utf-8") as f:
            json.dump(meta.to_dict(), f, indent=2)

        logger.debug("ArtifactStore: Stored %s (%d bytes, type=%s)", sha256[:16], len(data), content_type)
        return sha256

    async def retrieve(self, sha256: str) -> Optional[bytes]:
        filepath = self._artifact_path(sha256)
        if not os.path.exists(filepath):
            return None
        with open(filepath, "rb") as f:
            return f.read()

    async def get_metadata(self, sha256: str) -> Optional[ArtifactMetadata]:
        meta_path = self._meta_path(sha256)
        if not os.path.exists(meta_path):
            return None
        with open(meta_path, "r", encoding="utf-8") as f:
            return ArtifactMetadata.from_dict(json.load(f))

    async def list_by_job(self, job_id: str) -> List[ArtifactMetadata]:
        """Scan artifact directories for matching job_id metadata."""
        results = []
        prefix_dir = os.path.join(self.base_dir)
        if not os.path.exists(prefix_dir):
            return results

        for root, dirs, files in os.walk(prefix_dir):
            for fname in files:
                if fname.endswith(".meta.json"):
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            meta_dict = json.load(f)
                            if meta_dict.get("job_id") == job_id:
                                results.append(ArtifactMetadata.from_dict(meta_dict))
                    except (json.JSONDecodeError, OSError):
                        continue
        return results
