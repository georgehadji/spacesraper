# Regression tests for S4: engine.py used to navigate every fetch with
# wait_until="networkidle" and a 35s timeout. Any page with polling, ads,
# analytics beacons, or a websocket never reaches network idle, so every such
# fetch burned the full timeout. Default is now "load"; network_idle and
# wait_selector are opt-in per job, and a timeout on either is non-fatal.

import pytest

from src.infrastructure.browser.engine import ScraperEngine


class FakeResponse:
    ok = True
    status = 200


class FakePage:
    """Minimal Playwright Page stand-in: only what crawl() touches."""

    def __init__(self, idle_hangs: bool = False, selector_hangs: bool = False):
        self.idle_hangs = idle_hangs
        self.selector_hangs = selector_hangs
        self.goto_calls = []
        self.wait_for_load_state_calls = []
        self.wait_for_selector_calls = []
        self.url = "https://example.com"

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        return FakeResponse()

    async def wait_for_load_state(self, state, timeout=None):
        self.wait_for_load_state_calls.append({"state": state, "timeout": timeout})
        if self.idle_hangs:
            raise TimeoutError("networkidle never settled")

    async def wait_for_selector(self, selector, timeout=None):
        self.wait_for_selector_calls.append({"selector": selector, "timeout": timeout})
        if self.selector_hangs:
            raise TimeoutError("selector never appeared")

    async def title(self):
        return "OK"

    async def content(self):
        return "<html></html>"

    async def screenshot(self, path=None, full_page=None):
        return None


def _make_engine(page: FakePage) -> ScraperEngine:
    engine = ScraperEngine(context_pool=None, timeout=1000)
    engine.page = page
    return engine


@pytest.mark.asyncio
async def test_crawl_defaults_to_load_not_networkidle():
    page = FakePage()
    engine = _make_engine(page)

    payload = await engine.crawl("https://example.com")

    assert page.goto_calls[0]["wait_until"] == "load"
    assert page.wait_for_load_state_calls == []
    assert payload.error_message is None


@pytest.mark.asyncio
async def test_networkidle_timeout_is_non_fatal():
    """A page that never settles must still return a usable payload."""
    page = FakePage(idle_hangs=True)
    engine = _make_engine(page)

    payload = await engine.crawl("https://example.com", network_idle=True)

    assert page.wait_for_load_state_calls[0]["state"] == "networkidle"
    assert payload.error_message is None
    assert payload.html_content == "<html></html>"


@pytest.mark.asyncio
async def test_wait_selector_skips_networkidle_and_is_non_fatal():
    page = FakePage(selector_hangs=True)
    engine = _make_engine(page)

    payload = await engine.crawl(
        "https://example.com", network_idle=True, wait_selector=".ready"
    )

    assert page.wait_for_selector_calls[0]["selector"] == ".ready"
    assert page.wait_for_load_state_calls == [], "wait_selector should make networkidle redundant"
    assert payload.error_message is None
