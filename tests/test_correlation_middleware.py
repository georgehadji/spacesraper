# tests/test_correlation_middleware.py
import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from src.infrastructure.middleware.correlation import (
    CorrelationIDMiddleware,
    get_request_id,
)


@pytest.fixture
def app():
    test_app = FastAPI()
    test_app.add_middleware(CorrelationIDMiddleware)

    @test_app.get("/echo-id")
    async def echo():
        return {"request_id": get_request_id()}

    return test_app


@pytest.mark.asyncio
async def test_generates_request_id_when_absent(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/echo-id")
    assert response.status_code == 200
    rid = response.json()["request_id"]
    assert rid.startswith("req_")
    assert len(rid) == 12  # "req_" + 8 hex chars


@pytest.mark.asyncio
async def test_propagates_client_provided_id(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/echo-id", headers={"X-Request-ID": "my-trace-id"})
    assert response.json()["request_id"] == "my-trace-id"
    assert response.headers["X-Request-ID"] == "my-trace-id"


@pytest.mark.asyncio
async def test_request_id_in_response_header(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/echo-id")
    assert "X-Request-ID" in response.headers
