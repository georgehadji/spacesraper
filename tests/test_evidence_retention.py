# Regression tests for SEC-6: forensic screenshots need an off-by-default
# gate and a retention/purge policy — neither existed before.

import os
import time

from src.infrastructure.storage.evidence_retention import purge_expired_evidence


def test_missing_directory_purges_nothing(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert purge_expired_evidence(str(missing), retention_days=7) == 0


def test_purges_only_files_older_than_retention_window(tmp_path):
    old_file = tmp_path / "old_evidence.png"
    fresh_file = tmp_path / "fresh_evidence.png"
    old_file.write_bytes(b"old")
    fresh_file.write_bytes(b"fresh")

    ten_days_ago = time.time() - (10 * 86400)
    os.utime(old_file, (ten_days_ago, ten_days_ago))

    purged = purge_expired_evidence(str(tmp_path), retention_days=7)

    assert purged == 1
    assert not old_file.exists()
    assert fresh_file.exists()


def test_purges_nothing_within_retention_window(tmp_path):
    recent_file = tmp_path / "recent.png"
    recent_file.write_bytes(b"data")

    purged = purge_expired_evidence(str(tmp_path), retention_days=7)

    assert purged == 0
    assert recent_file.exists()
