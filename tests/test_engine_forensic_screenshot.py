# Regression test for SEC-6: forensic screenshots can contain personal data
# from the target page and must be off by default, opt-in via
# SCRAPER_FORENSIC_SCREENSHOTS.

import pytest

from src.infrastructure.browser.engine import ScraperEngine


class _FakePage:
    def __init__(self):
        self.screenshot_calls = []

    async def screenshot(self, path=None, full_page=None):
        self.screenshot_calls.append(path)


@pytest.mark.asyncio
async def test_forensic_screenshot_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SCRAPER_FORENSIC_SCREENSHOTS", raising=False)
    engine = ScraperEngine(context_pool=None)
    engine.page = _FakePage()

    await engine._capture_forensic_screenshot("https://example.com", "test_reason")

    assert engine.page.screenshot_calls == []


@pytest.mark.asyncio
async def test_forensic_screenshot_enabled_via_env_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRAPER_FORENSIC_SCREENSHOTS", "true")
    monkeypatch.chdir(tmp_path)
    engine = ScraperEngine(context_pool=None)
    engine.page = _FakePage()

    await engine._capture_forensic_screenshot("https://example.com", "test_reason")

    assert len(engine.page.screenshot_calls) == 1


@pytest.mark.asyncio
async def test_forensic_screenshot_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("SCRAPER_FORENSIC_SCREENSHOTS", "false")
    engine = ScraperEngine(context_pool=None)
    engine.page = _FakePage()

    await engine._capture_forensic_screenshot("https://example.com", "test_reason")

    assert engine.page.screenshot_calls == []
