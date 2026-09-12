"""P5 — browser pool resource ownership (D10, D11, D28).

Three findings from docs/plans/2026-09-11-precision-audit-remediation.md §9:

  D10  a context created then abandoned when add_init_script raises is
       referenced by nobody, so nothing ever closes it.
  D11  a raise inside initialize() leaves the launched Chromium running and
       unreferenced; the next acquire() re-enters initialize() and launches
       another one on top of it.
  D28  the warm-context queue was never leased from — every production
       caller goes through acquire(fingerprint=...), which builds a fresh
       persona-bound context — so the queue only ever held contexts that
       were created, replenished, and finally closed without being used.

Scope: these run against a fake Playwright, not a real browser. That is the
right level for all three — every one of them is about which objects this
module still holds a reference to on the failure path, which is observable
without launching Chromium. The live-browser contract is covered separately
by tests/integration/test_fingerprint_contract.py.
"""

import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.fingerprint import Fingerprint
from src.infrastructure.browser.pool import BrowserContextPool

POOL_SOURCE = Path(__file__).resolve().parents[1] / "src" / "infrastructure" / "browser" / "pool.py"


def make_fingerprint() -> Fingerprint:
    return Fingerprint(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36",
        platform="Win32",
        ua_platform="Windows",
        vendor="Google Inc. (Intel)",
        renderer="ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0)",
        viewport=(1920, 1080),
        screen=(1920, 1080),
        device_scale_factor=1,
        has_touch=False,
        hardware_concurrency=4,
        device_memory=8,
        locale="en-US",
        timezone="America/New_York",
    )


class _FakeContext:
    def __init__(self, init_script_error: Exception | None = None):
        self._init_script_error = init_script_error
        self.closed = False
        self.pages: list = []

    async def add_init_script(self, script: str) -> None:
        if self._init_script_error is not None:
            raise self._init_script_error

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, version: str = "131.0.6778.85", init_script_error: Exception | None = None):
        self.version = version
        self._init_script_error = init_script_error
        self.contexts: list[_FakeContext] = []
        self.closed = False

    async def new_context(self, **kwargs) -> _FakeContext:
        context = _FakeContext(self._init_script_error)
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, browsers: list[_FakeBrowser]):
        self._browsers = browsers
        self.launches = 0

    async def launch(self, **kwargs) -> _FakeBrowser:
        browser = self._browsers[min(self.launches, len(self._browsers) - 1)]
        self.launches += 1
        return browser


class _FakePlaywright:
    def __init__(self, browsers: list[_FakeBrowser]):
        self.chromium = _FakeChromium(browsers)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


@contextlib.contextmanager
def patched_playwright(fake: _FakePlaywright):
    """async_playwright() returns a starter whose .start() yields the driver."""
    starter = MagicMock()
    starter.start = AsyncMock(return_value=fake)
    with patch("src.infrastructure.browser.pool.async_playwright", return_value=starter):
        yield


# --- D10 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_context_orphaned_by_a_failing_init_script_is_closed():
    """The guard for D10.

    new_context() has already created a real context in the browser process
    by the time add_init_script runs. If that raises and the exception is
    allowed to propagate as-is, the only reference to the context dies with
    the stack frame — close_all() cannot reach it, and it holds its share of
    Chromium memory until the process exits.
    """
    browser = _FakeBrowser(init_script_error=RuntimeError("init script rejected"))
    pool = BrowserContextPool()
    pool._browser = browser

    with pytest.raises(RuntimeError, match="init script rejected"):
        await pool._create_stealth_context(make_fingerprint())

    assert len(browser.contexts) == 1, "expected exactly one context to have been created"
    assert browser.contexts[0].closed, "the orphaned context was never closed"


@pytest.mark.asyncio
async def test_b_a_successful_context_is_not_closed():
    """Control for the D10 guard: the cleanup must be on the failure path only."""
    browser = _FakeBrowser()
    pool = BrowserContextPool()
    pool._browser = browser

    context = await pool._create_stealth_context(make_fingerprint())

    assert context is browser.contexts[0]
    assert not context.closed


# --- D11 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c_a_failed_initialize_leaves_no_chromium_running():
    """The guard for D11.

    Anything that raises between launch() and the end of initialize() leaves
    self._browser pointing at a live Chromium while _is_initialized stays
    False. An unparseable version string is the surviving window for that
    today; the property under test is the failure path's cleanup, not which
    statement happens to raise.
    """
    browser = _FakeBrowser(version="not-a-version")
    fake = _FakePlaywright([browser])
    pool = BrowserContextPool()

    with patched_playwright(fake), pytest.raises(ValueError):
        await pool.initialize()

    assert browser.closed, "the launched Chromium was left running after initialize() failed"
    assert fake.stopped, "the Playwright driver was left running after initialize() failed"
    assert pool._browser is None, "the pool still references a browser it did not finish building"
    assert pool._is_initialized is False


@pytest.mark.asyncio
async def test_d_a_retry_after_a_failed_initialize_does_not_stack_chromiums():
    """A failed initialize() must not cost a leaked browser per attempt.

    This is the consequence the finding names: acquire() auto-initializes, so
    every subsequent job re-enters initialize() and launches another Chromium
    on top of the one the previous attempt abandoned.
    """
    first = _FakeBrowser(version="not-a-version")
    second = _FakeBrowser()
    fake = _FakePlaywright([first, second])
    pool = BrowserContextPool()

    with patched_playwright(fake):
        with pytest.raises(ValueError):
            await pool.initialize()
        await pool.initialize()

        assert fake.chromium.launches == 2
        assert first.closed, "the first attempt's browser is still running alongside the second"
        assert pool._browser is second
        assert pool._is_initialized is True

        context = await pool.acquire(make_fingerprint())
        assert context in second.contexts


# --- D28 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e_initialize_creates_no_contexts_nobody_leases():
    """The guard for D28.

    Pre-filling a warm queue cost one context per configured slot at startup,
    kept them resident for the life of the process, replenished them on a
    timer, and closed them at shutdown — and no caller ever took one, because
    every caller needs a persona-bound context that cannot be retrofitted
    onto an already-created one.
    """
    browser = _FakeBrowser()
    fake = _FakePlaywright([browser])
    pool = BrowserContextPool()

    with patched_playwright(fake):
        await pool.initialize()

    assert browser.contexts == [], "initialize() built contexts no caller can lease"
    assert pool.chromium_major == 131


@pytest.mark.asyncio
async def test_f_release_closes_the_context_rather_than_parking_it():
    """Every context is persona-bound now, so release is a close, not a return."""
    browser = _FakeBrowser()
    fake = _FakePlaywright([browser])
    pool = BrowserContextPool()

    with patched_playwright(fake):
        await pool.initialize()
        context = await pool.acquire(make_fingerprint())
        await pool.release(context)

    assert context.closed
    metrics = await pool.get_metrics()
    assert metrics["contexts_created"] == 1


@pytest.mark.asyncio
async def test_g_close_all_shuts_down_the_browser_and_the_driver():
    browser = _FakeBrowser()
    fake = _FakePlaywright([browser])
    pool = BrowserContextPool()

    with patched_playwright(fake):
        await pool.initialize()
        await pool.close_all()

    assert browser.closed
    assert fake.stopped
    assert pool._is_initialized is False


def test_h_no_lease_queue_machinery_survives():
    """Source-drift guard for D28.

    Checked against code tokens rather than prose: the comments in pool.py
    legitimately describe the queue that used to be there, and a test that
    matched those would fail on the explanation of its own fix.
    """
    source = POOL_SOURCE.read_text(encoding="utf-8")
    for token in (
        "self._context_queue",
        "self.pool_size",
        "_should_recycle_context",
        "self._health_check_task",
        "_contexts_recycled",
    ):
        assert token not in source, f"{token} is dead queue machinery and should be gone"
