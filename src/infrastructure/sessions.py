# P3: SessionPool — leases persona+proxy pairs per domain, scores outcomes,
# retires blocked/exhausted sessions. Closes the loop Crawlee's SessionPool
# models: never rotate a persona without its proxy, or vice versa.
#
# In-memory, per-process — no cross-restart cookie/session persistence.
# Matches this plan's existing worker-state precedent (domain_endpoints,
# the P2 crawl-scoped seen-set); a durable SessionRepository (SQLite/Valkey)
# is the upgrade path if a target needs sessions to survive a worker
# restart, e.g. to keep a login cookie jar warm.
# docs/plans/2026-08-13-capability-enhancement-plan.md P3.

import logging
import uuid

from src.domain.ports import ProxyProviderPort
from src.domain.session import Session
from src.infrastructure.monitoring.observability import metrics_tracker

logger = logging.getLogger("Spacescraper.SessionPool")

# How often (in release() calls, across all domains) to sweep fully-retired
# domain buckets out of the pool. Not every-call: the sweep is O(domains),
# and this is a background hygiene pass, not a correctness requirement.
SWEEP_INTERVAL_RELEASES = 50


class SessionPool:
    def __init__(self, proxy_provider: ProxyProviderPort):
        self.proxy_provider = proxy_provider
        self._sessions: dict[str, list[Session]] = {}  # domain -> live sessions
        # Sessions currently on loan, by domain. A session in this set is
        # invisible to lease() even though it's still in self._sessions — this
        # is what stops two concurrent leases on one domain from being handed
        # the same Session (the rate limiter allows up to `default_budget`
        # concurrent jobs per domain, so this is a real, not hypothetical,
        # race). Cleared on release(), regardless of outcome.
        self._checked_out: dict[str, set[str]] = {}
        self._sweep_counter = 0

    def lease(self, domain: str) -> Session:
        """Reuses the healthiest non-retired, not-already-checked-out session
        for this domain, or mints a new persona+proxy pair. Persona and proxy
        are bound for the session's whole lifetime — a fresh lease never mixes
        an old proxy with a new persona or vice versa.

        A session returned here stays checked out until release() — call it
        exactly once per lease(), on every path (success, failure, and a crash
        before either is known), or the session is stranded unavailable for
        this domain until the process restarts."""
        checked_out = self._checked_out.get(domain, set())
        live = [
            s for s in self._sessions.get(domain, [])
            if not s.retired and s.session_id not in checked_out
        ]
        if live:
            session = max(live, key=lambda s: s.health_score)
        else:
            session = Session(
                session_id=f"sess_{uuid.uuid4().hex[:12]}",
                persona_id=f"persona_{uuid.uuid4().hex[:12]}",
                proxy=self.proxy_provider.next_proxy(),
            )
            self._sessions.setdefault(domain, []).append(session)
        self._checked_out.setdefault(domain, set()).add(session.session_id)
        return session

    async def release(self, domain: str, session: Session, *, success: bool, blocked: bool) -> Session:
        """Replaces the pool's copy with the scored outcome (immutability —
        never mutate the leased Session in place), checks it back in so a
        later lease() can hand it out again, and retires/evicts it from
        future leases if the score/use-count crossed the line.

        Safe to call on a session that was never checked out (discard is a
        no-op) — a caller that releases the same Session object twice after
        one lease() should still fix that at the call site; this method will
        not raise for it, but it will re-score from the caller's stale copy
        each time."""
        updated = session.score_outcome(success=success, blocked=blocked)
        bucket = self._sessions.setdefault(domain, [])
        for i, existing in enumerate(bucket):
            if existing.session_id == session.session_id:
                bucket[i] = updated
                break
        else:
            bucket.append(updated)
        self._checked_out.get(domain, set()).discard(session.session_id)

        if updated.retired:
            logger.info("SessionPool: retiring %s for %s (score=%.1f, uses=%d)",
                        updated.session_id, domain, updated.health_score, updated.uses)
            await metrics_tracker.increment("session_retirements")

        self._sweep_if_due()
        return updated

    def _sweep_if_due(self) -> None:
        """Drops domains whose entire bucket has retired, and un-tracks their
        (by then empty) checked-out set. self._sessions has no TTL/eviction
        otherwise and would grow one bucket per domain ever scraped for the
        life of the worker process; a fully-retired bucket is pure memory
        with no upside — lease() mints a fresh session for that domain next
        time regardless."""
        self._sweep_counter += 1
        if self._sweep_counter % SWEEP_INTERVAL_RELEASES != 0:
            return
        for domain in [d for d, bucket in self._sessions.items() if all(s.retired for s in bucket)]:
            del self._sessions[domain]
            self._checked_out.pop(domain, None)
