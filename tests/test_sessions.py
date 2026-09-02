# P3: Session value object + SessionPool — health scoring, retirement,
# persona/proxy coherence across lease/release.
# docs/plans/2026-08-13-capability-enhancement-plan.md P3 deliverables.

import pytest

from src.domain.session import MAX_USES, RETIREMENT_THRESHOLD, Session
from src.infrastructure.sessions import SessionPool


class FakeProxyProvider:
    def __init__(self, proxies):
        self._proxies = list(proxies)
        self._i = 0

    def next_proxy(self):
        if not self._proxies:
            return None
        p = self._proxies[self._i % len(self._proxies)]
        self._i += 1
        return p


def test_score_outcome_returns_new_session_never_mutates():
    original = Session(session_id="s1", persona_id="p1")
    updated = original.score_outcome(success=True, blocked=False)

    assert original.health_score == 0.0  # unchanged
    assert original.uses == 0
    assert updated.health_score == 1.0
    assert updated.uses == 1
    assert updated is not original


def test_session_retires_after_enough_blocks():
    s = Session(session_id="s1", persona_id="p1")
    for _ in range(3):
        s = s.score_outcome(success=False, blocked=True)
    assert s.health_score <= RETIREMENT_THRESHOLD
    assert s.retired is True


def test_session_retires_at_max_uses_even_with_good_score():
    s = Session(session_id="s1", persona_id="p1")
    for _ in range(MAX_USES):
        s = s.score_outcome(success=True, blocked=False)
    assert s.retired is True


def test_session_pool_lease_binds_persona_and_proxy_together():
    pool = SessionPool(FakeProxyProvider(["http://proxy1:8080"]))
    session = pool.lease("example.com")
    assert session.persona_id
    assert session.proxy == "http://proxy1:8080"


@pytest.mark.asyncio
async def test_session_pool_reuses_same_session_across_leases_until_retired():
    pool = SessionPool(FakeProxyProvider(["http://proxy1:8080"]))
    first = pool.lease("example.com")
    await pool.release("example.com", first, success=True, blocked=False)
    second = pool.lease("example.com")
    assert first.session_id == second.session_id


@pytest.mark.asyncio
async def test_concurrent_leases_on_same_domain_get_different_sessions():
    """R4: two jobs racing the same domain (the rate limiter allows this,
    default_budget=2) must not be handed the same Session — each lease()
    checks a session out until its own release()."""
    pool = SessionPool(FakeProxyProvider(["http://proxy1:8080", "http://proxy2:8080"]))
    first = pool.lease("example.com")
    second = pool.lease("example.com")  # first is still checked out
    assert first.session_id != second.session_id

    updated_first = await pool.release("example.com", first, success=True, blocked=False)
    updated_second = await pool.release("example.com", second, success=False, blocked=True)
    # each release scored its own session — neither outcome overwrote the other
    assert updated_first.uses == 1
    assert updated_first.health_score == 1.0  # success delta
    assert updated_second.uses == 1
    assert updated_second.health_score == -3.0  # block delta


@pytest.mark.asyncio
async def test_blocked_session_retires_and_is_not_leased_again():
    pool = SessionPool(FakeProxyProvider(["http://proxy1:8080"]))
    session = pool.lease("example.com")

    updated = session
    for _ in range(3):
        updated = await pool.release("example.com", updated, success=False, blocked=True)
    assert updated.retired

    next_lease = pool.lease("example.com")
    assert next_lease.session_id != session.session_id, "a retired session must not be re-leased"


@pytest.mark.asyncio
async def test_release_persists_updated_score_for_next_lease():
    pool = SessionPool(FakeProxyProvider(["http://proxy1:8080"]))
    session = pool.lease("example.com")
    await pool.release("example.com", session, success=True, blocked=False)

    released = pool.lease("example.com")
    assert released.health_score == 1.0
    assert released.uses == 1


@pytest.mark.asyncio
async def test_persona_proxy_binding_survives_lease_and_release_cycle():
    """The same persona_id/proxy pair must come back on every lease of the
    same live session — never rotate one without the other."""
    pool = SessionPool(FakeProxyProvider(["http://proxy1:8080", "http://proxy2:8080"]))
    original = pool.lease("example.com")
    await pool.release("example.com", original, success=True, blocked=False)

    for _ in range(5):
        leased = pool.lease("example.com")
        assert leased.persona_id == original.persona_id
        assert leased.proxy == original.proxy
        leased = await pool.release("example.com", leased, success=True, blocked=False)


def test_no_configured_proxies_yields_none_without_error():
    pool = SessionPool(FakeProxyProvider([]))
    session = pool.lease("example.com")
    assert session.proxy is None
    assert session.persona_id
