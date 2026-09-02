# P2: HttpRobotsGate — fail-closed on fetch error, allow-all on 404.
# docs/plans/2026-08-13-capability-enhancement-plan.md P2 (R4).

from unittest.mock import AsyncMock

import pytest

from src.infrastructure.robots import HttpRobotsGate


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


@pytest.mark.asyncio
async def test_disallowed_path_is_blocked(monkeypatch):
    gate = HttpRobotsGate()
    monkeypatch.setattr(
        "src.infrastructure.robots.target_http.get",
        AsyncMock(return_value=_FakeResponse(200, "User-agent: *\nDisallow: /private/\n")),
    )
    assert await gate.is_allowed("https://example.com/private/x") is False
    assert await gate.is_allowed("https://example.com/public") is True


@pytest.mark.asyncio
async def test_missing_robots_txt_allows_everything(monkeypatch):
    gate = HttpRobotsGate()
    monkeypatch.setattr(
        "src.infrastructure.robots.target_http.get",
        AsyncMock(return_value=_FakeResponse(404)),
    )
    assert await gate.is_allowed("https://example.com/anything") is True


@pytest.mark.asyncio
async def test_fetch_failure_fails_closed(monkeypatch):
    gate = HttpRobotsGate()
    monkeypatch.setattr(
        "src.infrastructure.robots.target_http.get",
        AsyncMock(side_effect=ConnectionError("dns failure")),
    )
    assert await gate.is_allowed("https://example.com/x") is False


@pytest.mark.asyncio
async def test_server_error_fails_closed(monkeypatch):
    gate = HttpRobotsGate()
    monkeypatch.setattr(
        "src.infrastructure.robots.target_http.get",
        AsyncMock(return_value=_FakeResponse(503)),
    )
    assert await gate.is_allowed("https://example.com/x") is False


@pytest.mark.asyncio
async def test_crawl_delay_parsed(monkeypatch):
    gate = HttpRobotsGate()
    monkeypatch.setattr(
        "src.infrastructure.robots.target_http.get",
        AsyncMock(return_value=_FakeResponse(200, "User-agent: *\nCrawl-delay: 5\n")),
    )
    assert await gate.crawl_delay_seconds("https://example.com/x") == 5.0


@pytest.mark.asyncio
async def test_result_cached_across_calls(monkeypatch):
    gate = HttpRobotsGate()
    mock_get = AsyncMock(return_value=_FakeResponse(200, "User-agent: *\nDisallow: /a\n"))
    monkeypatch.setattr("src.infrastructure.robots.target_http.get", mock_get)

    await gate.is_allowed("https://example.com/x")
    await gate.is_allowed("https://example.com/y")

    assert mock_get.call_count == 1
