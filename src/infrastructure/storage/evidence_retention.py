# SEC-6: forensic screenshots (engine.py's _capture_forensic_screenshot)
# accumulate unbounded in exports/evidence/ and can carry personal data from
# the target page. This purges files older than a retention window — same
# retention_days -> purged-count shape as JobRepository/RecordRepository's
# purge_expired_*() (src/domain/ports.py), applied to files instead of DB
# rows since evidence isn't tracked in a table.
#
# Nothing in this codebase runs a scheduler yet (apscheduler is a listed
# dependency but unused in src/) — the P0 job/record reaper this mirrors
# isn't wired to automatic invocation either. Call this from an external
# cron/scheduled task the same way DEPLOYMENT.md already recommends
# logrotate for logs/trace.log.

import logging
import os
import time

logger = logging.getLogger("Spacescraper.EvidenceRetention")

DEFAULT_EVIDENCE_DIR = "exports/evidence"
DEFAULT_RETENTION_DAYS = 7


def purge_expired_evidence(
    directory: str = DEFAULT_EVIDENCE_DIR, retention_days: int = DEFAULT_RETENTION_DAYS
) -> int:
    """Delete files in `directory` last modified more than `retention_days`
    ago. Returns the count purged; missing directory is not an error."""
    if not os.path.isdir(directory):
        return 0

    cutoff = time.time() - (retention_days * 86400)
    purged = 0
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                purged += 1
        except OSError as e:
            logger.debug("Evidence purge: failed to remove %s: %s", path, e)

    if purged:
        logger.info("Evidence purge: removed %d expired file(s) from %s", purged, directory)
    return purged
