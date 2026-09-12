"""P6 — two wiring defects that each silently produce a wrong value (D23, D24).

  D23  CorrelationIDMiddleware is defined, documented, and imported from --
       but never added to the app. get_request_id() therefore returned ""
       for every HTTP request, so every job row was written with
       correlation_id=None and end-to-end tracing has never worked from the
       API side. Nothing failed; the field was just always empty.

  D24  spacescraper.py reads source['target_site'] to build the job id and
       source.get('target_site', 'universal') four lines later. A source
       entry that omits the optional key raises KeyError on line one of the
       two — the file contradicts itself within the same statement block.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import main
from src.infrastructure.middleware.correlation import (
    CorrelationIDMiddleware,
    get_request_id,
)


# --- D23 -------------------------------------------------------------------


def test_a_the_correlation_middleware_is_actually_installed():
    """The guard for D23, at the only place it can be checked cheaply.

    The class existing and being imported from proves nothing — that was
    already true while every correlation id was None.
    """
    installed = [m.cls for m in main.app.user_middleware]
    assert CorrelationIDMiddleware in installed, (
        "CorrelationIDMiddleware is defined but never added to the app"
    )


def test_b_an_incoming_request_id_reaches_the_handler():
    """The consequence: a caller's id must be readable where jobs are built.

    main.py's submit handler does `correlation_id = get_request_id() or None`,
    so this asserts on exactly the call that was always returning "".
    """
    seen = {}

    @main.app.get("/__p6_correlation_probe")
    async def _probe():
        seen["id"] = get_request_id()
        return {"ok": True}

    try:
        with TestClient(main.app) as client:
            response = client.get(
                "/__p6_correlation_probe", headers={"X-Request-ID": "req_from_caller"}
            )
    finally:
        main.app.router.routes = [
            r for r in main.app.router.routes
            if getattr(r, "path", None) != "/__p6_correlation_probe"
        ]

    assert seen.get("id") == "req_from_caller"
    assert response.headers["X-Request-ID"] == "req_from_caller"


def test_c_a_request_without_an_id_still_gets_one():
    """Control: the middleware generates an id rather than leaving it blank."""
    seen = {}

    @main.app.get("/__p6_correlation_probe2")
    async def _probe():
        seen["id"] = get_request_id()
        return {"ok": True}

    try:
        with TestClient(main.app) as client:
            response = client.get("/__p6_correlation_probe2")
    finally:
        main.app.router.routes = [
            r for r in main.app.router.routes
            if getattr(r, "path", None) != "/__p6_correlation_probe2"
        ]

    assert seen.get("id", "").startswith("req_")
    assert response.headers["X-Request-ID"] == seen["id"]


# --- D24 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d_a_source_without_target_site_is_seeded_not_crashed(tmp_path, monkeypatch):
    """The guard for D24.

    target_site is optional everywhere else in this block — the ScrapeJob
    below defaults it. Only the job id treated it as required, so a
    perfectly valid sources.yaml entry took the seeder down with a KeyError
    before a single job was pushed.
    """
    import spacescraper

    (tmp_path / "sources.yaml").write_text(
        "sources:\n"
        "  - start_urls:\n"
        "      - https://example.invalid/a\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    queue = AsyncMock()
    await spacescraper.seed_jobs_from_config(queue)

    queue.push.assert_called_once()
    stream, envelope = queue.push.call_args.args
    assert stream == "jobs_stream"
    assert envelope.payload["url"] == "https://example.invalid/a"
    assert envelope.payload["target_site"] == "universal", (
        "the default the next line already applies must also apply to the job id"
    )


@pytest.mark.asyncio
async def test_e_an_explicit_target_site_is_still_honoured(tmp_path, monkeypatch):
    """Control: the default must not override a configured value."""
    import spacescraper

    (tmp_path / "sources.yaml").write_text(
        "sources:\n"
        "  - target_site: procurement\n"
        "    start_urls:\n"
        "      - https://example.invalid/b\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    queue = AsyncMock()
    await spacescraper.seed_jobs_from_config(queue)

    envelope = queue.push.call_args.args[1]
    assert envelope.payload["target_site"] == "procurement"
    assert envelope.payload["job_id"] == "init_procurement"
