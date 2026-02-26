"""
Filesystem Storage Adapter — Append-Only Persistence Layer
Content-addressed immutable artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, Optional


class FilesystemStorage:
    """
    Append-only storage. Artifacts are content-addressed and immutable.
    URI format: storage://<sha256_prefix>/<filename>
    """

    def __init__(self, base_dir: str = "/tmp/scraper_storage") -> None:
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    # --- Write ---

    def write(self, content: str, filename: str) -> str:
        """Write content and return URI. Never overwrites existing content."""
        sha = hashlib.sha256(content.encode()).hexdigest()[:16]
        dir_path = os.path.join(self.base_dir, sha)
        os.makedirs(dir_path, exist_ok=True)
        file_path = os.path.join(dir_path, filename)
        if not os.path.exists(file_path):
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
        return f"storage://{sha}/{filename}"

    def write_json(self, data: Any, filename: str) -> str:
        return self.write(json.dumps(data, indent=2, default=str), filename)

    # --- Read ---

    def read(self, uri: str) -> str:
        path = self._uri_to_path(uri)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def read_json(self, uri: str) -> Any:
        return json.loads(self.read(uri))

    def exists(self, uri: str) -> bool:
        try:
            return os.path.exists(self._uri_to_path(uri))
        except Exception:
            return False

    # --- Internal ---

    def _uri_to_path(self, uri: str) -> str:
        # storage://sha/filename -> base_dir/sha/filename
        without_scheme = uri.replace("storage://", "")
        return os.path.join(self.base_dir, without_scheme)


# Module-level singleton
_storage: Optional[FilesystemStorage] = None


def get_storage(base_dir: str = "/tmp/scraper_storage") -> FilesystemStorage:
    global _storage
    if _storage is None:
        _storage = FilesystemStorage(base_dir)
    return _storage
