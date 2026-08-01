# API gateway smoke test.
# main.py had shipped with two module-level NameErrors because nothing ever
# imported or booted it. This test boots the real app and walks the job
# lifecycle, so an import or startup regression fails the suite.

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    # main.py builds its repositories at module scope with relative db paths,
    # so the working directory has to be set before it is imported.
    monkeypatch.chdir(tmp_path)
    main = importlib.import_module("main")
    with TestClient(main.app) as c:
        yield c


def test_health_is_reachable(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_demo_key_not_advertised_without_config(client):
    # verify_api_key only honours a demo key in development with DEMO_API_KEY set;
    # handing out a default would advertise a key that every request rejects.
    assert client.get("/demo/key").status_code == 404


def test_job_lifecycle_over_http(client):
    registered = client.post("/auth/register", json={"email": "smoke@example.com", "tier": "pro"})
    assert registered.status_code == 200
    headers = {"Authorization": f"Bearer {registered.json()['api_key']}"}

    # Rate limiting must degrade to a local counter when Redis is down,
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
    registered = client.post("/auth/register", json={"email": "smoke@example.com", "tier": "pro"})
    headers = {"Authorization": f"Bearer {registered.json()['api_key']}"}
    blocked = client.post("/jobs", json={"url": "http://127.0.0.1:8000/admin"}, headers=headers)
    assert blocked.status_code == 400
