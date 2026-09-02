# Contract test for S1: a launched context's navigator.userAgent,
# navigator.platform, Sec-CH-UA-Platform, screen.width, and the HTTP
# User-Agent header must all agree, and the context's init script must not
# throw (a throwing init script is how the original defect went unnoticed —
# see docs/plans/2026-08-19-scrapling-informed-hardening-plan.md S1).

import pytest

from src.infrastructure.browser.pool import BrowserContextPool
from src.infrastructure.browser.persona import persona_manager


@pytest.mark.asyncio
async def test_persona_bound_context_is_internally_coherent():
    pool = BrowserContextPool(pool_size=1, headless=True)
    try:
        try:
            await pool.initialize()
        except Exception as e:
            # CI installs chromium (see .github/workflows/ci.yml), so this
            # should run there. On a machine without the browser binary,
            # skip rather than fail: a missing browser is an environment gap,
            # not a defect in the contract under test. Skips stay visible in
            # the run summary, so this can't quietly stop covering S1.
            if "Executable doesn't exist" in str(e) or "playwright install" in str(e).lower():
                pytest.skip(f"Playwright browser not installed: {e}")
            raise
        fingerprint = persona_manager.generate_fingerprint("contract-test", pool.chromium_major)
        context = await pool.acquire(fingerprint=fingerprint)

        page_errors = []
        page = await context.new_page()
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        captured_headers = {}

        async def capture_and_fulfill(route):
            # Fulfill locally rather than continue_() — hermetic, no real
            # network call, but the request (and its headers) is still real.
            captured_headers.update(route.request.headers)
            await route.fulfill(status=200, content_type="text/html", body="<html></html>")

        await page.route("**/*", capture_and_fulfill)
        await page.goto("https://fingerprint-contract-test.invalid/", wait_until="load", timeout=15000)

        nav_ua = await page.evaluate("navigator.userAgent")
        nav_platform = await page.evaluate("navigator.platform")
        screen_width = await page.evaluate("screen.width")
        ua_platform = await page.evaluate(
            "navigator.userAgentData ? navigator.userAgentData.platform : null"
        )

        assert page_errors == [], f"init script threw: {page_errors}"
        assert nav_ua == fingerprint.user_agent
        assert nav_platform == fingerprint.platform
        assert screen_width == fingerprint.viewport[0]
        assert captured_headers.get("user-agent") == fingerprint.user_agent
        if ua_platform is not None:
            assert ua_platform == fingerprint.ua_platform

        await context.close()
    finally:
        await pool.close_all()
