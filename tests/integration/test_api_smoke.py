# API gateway smoke test.
# main.py had shipped with two module-level NameErrors because nothing ever
# imported or booted it. This test boots the real app and walks the job
# lifecycle, so an import or startup regression fails the suite.

import importlib

import pytest
from fastapi.testclient import TestClient

ADMIN_KEY = "smoke-test-admin-key"
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    # main.py builds its repositories at module scope with relative db paths,
    # so the working directory has to be set before it is imported.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)
    main = importlib.import_module("main")
    # main.api_key_manager is a process-wide singleton and importlib caches
    # the module, so its in-memory rate-limit counters and key store would
    # otherwise bleed across tests in this file. Reset for isolation.
    main.api_key_manager._local_counts.clear()
    main.api_key_manager._keys_by_hash.clear()
    with TestClient(main.app) as c:
        yield c


def _register(client, **overrides):
    payload = {"email": "smoke@example.com", "tier": "pro"}
    payload.update(overrides)
    return client.post("/auth/register", json=payload, headers=ADMIN_HEADERS)


def test_health_is_reachable(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_demo_key_not_advertised_without_config(client):
    # verify_api_key only honours a demo key in development with DEMO_API_KEY set;
    # handing out a default would advertise a key that every request rejects.
    assert client.get("/demo/key").status_code == 404


def test_job_lifecycle_over_http(client):
    registered = _register(client)
    assert registered.status_code == 200
    headers = {"Authorization": f"Bearer {registered.json()['api_key']}"}

    # Rate limiting must degrade to a local counter when Valkey is down,
    # not fail the request.
    submitted = client.post("/jobs", json={"url": "https://example.com/listing"}, headers=headers)
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]

    detail = client.get(f"/jobs/{job_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["state"] == "QUEUED"

    records = client.get(f"/jobs/{job_id}/records", headers=headers)
    assert records.status_code == 200
    assert records.json()["records"] == []

    cancelled = client.post(f"/jobs/{job_id}/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_unauthenticated_submission_is_rejected(client):
    assert client.post("/jobs", json={"url": "https://example.com"}).status_code == 401


def test_ssrf_guard_rejects_internal_target(client):
    registered = _register(client)
    headers = {"Authorization": f"Bearer {registered.json()['api_key']}"}
    blocked = client.post("/jobs", json={"url": "http://127.0.0.1:8000/admin"}, headers=headers)
    assert blocked.status_code == 400


def test_anonymous_registration_is_rejected(client):
    # F11: POST /auth/register previously minted enterprise-tier keys for
    # any anonymous caller. It must now require an admin key.
    response = client.post("/auth/register", json={"email": "attacker@example.com", "tier": "enterprise"})
    assert response.status_code == 401


def test_registration_with_wrong_admin_key_is_rejected(client):
    response = client.post(
        "/auth/register",
        json={"email": "attacker@example.com", "tier": "enterprise"},
        headers={"Authorization": "Bearer not-the-admin-key"},
    )
    assert response.status_code == 401


def test_outbox_relay_delivers_pending_job_submitted_event(client):
    # C5: OutboxRelay.run_forever() previously was never started anywhere,
    # so outbox rows accumulated and were never relayed. Verify a job
    # submission's outbox event is actually delivered, not just written.
    import main

    registered = _register(client)
    headers = {"Authorization": f"Bearer {registered.json()['api_key']}"}
    submitted = client.post("/jobs", json={"url": "https://example.com/listing"}, headers=headers)
    assert submitted.status_code == 202, submitted.text

    # TestClient's lifespan runs on a portal thread with its own event loop;
    # aiosqlite connections are bound to it, so drive these calls through the
    # same portal instead of asyncio.run() (which would use a different loop).
    assert client.portal.call(main.container.outbox_repo.get_pending_count) >= 1
    delivered_count = client.portal.call(main.container.outbox_relay.run_once)
    assert delivered_count >= 1
    assert client.portal.call(main.container.outbox_repo.get_pending_count) == 0


def test_job_submission_rolls_back_on_outbox_write_failure(client):
    # F14/W3.7: create_job() and create_outbox_event() used to run on two
    # separate connections/transactions, so a failure between them could
    # orphan a job with no outbox event. They now share job_repo's
    # connection and one transaction — simulate a failure on the second
    # write and confirm the first is rolled back too, not orphaned.
    #
    # W4.5: the outbox repo is swapped via FastAPI's dependency_overrides
    # (an injected fake) instead of monkeypatching a method on the shared
    # singleton. job_repo is not overridden, so it's still the real, shared
    # instance and the rollback path under test is exercised for real.
    import main

    registered = _register(client)
    headers = {"Authorization": f"Bearer {registered.json()['api_key']}"}

    class _FailingOutboxRepo:
        async def create_event(self, *args, **kwargs):
            raise RuntimeError("simulated outbox write failure")

    main.app.dependency_overrides[main.get_outbox_repo] = lambda: _FailingOutboxRepo()
    try:
        with pytest.raises(RuntimeError, match="simulated outbox write failure"):
            client.post("/jobs", json={"url": "https://example.com/listing"}, headers=headers)
    finally:
        del main.app.dependency_overrides[main.get_outbox_repo]

    jobs_after = client.portal.call(
        main.container.job_repo._conn.execute_fetchall, "SELECT job_id FROM jobs"
    )
    assert jobs_after == []
    assert client.portal.call(main.container.outbox_repo.get_pending_count) == 0


def test_health_reflects_real_degraded_metrics(client):
    # F16/W3.9: /health used to return a hardcoded sample_metrics dict, so it
    # reported a constant "healthy" through a total outage. It now reads
    # metrics_tracker, so a real run of failures must flip it to degraded.
    import main

    healthy_before = client.get("/health")
    assert healthy_before.status_code == 200
    assert healthy_before.json()["status"] == "healthy"

    client.portal.call(main.metrics_tracker.record_job_status, False)

    degraded = client.get("/health")
    assert degraded.status_code == 200
    body = degraded.json()
    assert body["status"] == "degraded"
    assert any(a["name"] == "extraction_success_rate" for a in body["slo_alerts"])


def test_overlay_promotion_uses_shared_repo_connection(client):
    # C13/W4.3: promote_overlay used to construct+init+close its own
    # SqliteOverlayRepository per request instead of using the module-level
    # singleton every other endpoint shares. Verify promotion still works
    # end-to-end now that it reuses the shared container.overlay_repo.
    import main
    from src.domain.models import ExtractionOverlay

    registered = _register(client)
    headers = {"Authorization": f"Bearer {registered.json()['api_key']}"}

    overlay = ExtractionOverlay(
        overlay_id="ov-smoke-1", domain="example.com", schema_id="s1",
    )
    client.portal.call(main.container.overlay_repo.create_overlay, overlay)

    response = client.post(
        "/overlays/ov-smoke-1/promote",
        json={"target_state": "shadow"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["new_state"] == "SHADOW"

    stored = client.portal.call(main.container.overlay_repo.get_overlay, "ov-smoke-1")
    assert stored.state.value == "SHADOW"


def test_registration_is_rate_limited_per_ip(client):
    from src.auth_middleware import REGISTRATION_IP_LIMIT

    for i in range(REGISTRATION_IP_LIMIT):
        response = _register(client, email=f"smoke{i}@example.com")
        assert response.status_code == 200, response.text

    throttled = _register(client, email="one-too-many@example.com")
    assert throttled.status_code == 429
