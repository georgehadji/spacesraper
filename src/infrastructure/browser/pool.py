# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Browser Orchestration)
# Role: Manages a high-performance pool of isolated Playwright browser contexts.

import asyncio
import contextlib
import logging
import os
from urllib.parse import unquote, urlsplit

from playwright.async_api import Browser, BrowserContext, async_playwright

from src.domain.fingerprint import Fingerprint

# Initialize localized logger for browser cluster telemetry
logger = logging.getLogger("Spacescraper.BrowserPool")


def parse_proxy_url(proxy: str) -> dict[str, str]:
    """Split a 'scheme://[user:pass@]host[:port]' proxy URL (SessionPool's
    format, e.g. 'http://user:pass@ip:port') into Playwright's ProxySettings
    shape: {'server': ..., 'username'?: ..., 'password'?: ...}.

    R6/R-W4: curl_cffi (Tier 1) accepts the whole URL string with embedded
    userinfo natively, but Playwright's `server` field does not — passing
    the raw string as `server` silently drops the credentials and the proxy
    answers 407. This is the one place that matters, so parsing happens
    here at the point of consumption rather than changing the string
    format SessionPool/StaticProxyProvider hand out (which Tier 1 already
    consumes correctly as-is)."""
    parts = urlsplit(proxy)
    server = f"{parts.scheme}://{parts.hostname}"
    if parts.port:
        server += f":{parts.port}"
    result = {"server": server}
    if parts.username:
        # urlsplit does not decode percent-escapes in userinfo (that's
        # unquote's job) — Playwright's proxy auth wants the literal
        # credential, not its URL-encoded form.
        result["username"] = unquote(parts.username)
    if parts.password:
        result["password"] = unquote(parts.password)
    return result


def _running_in_container() -> bool:
    """Best-effort Docker/Kubernetes detection (cgroup v1 and v2)."""
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup") as f:
            content = f.read()
        return "docker" in content or "kubepods" in content
    except OSError:
        return False


def _sandbox_should_be_disabled() -> bool:
    """
    SEC-5: the Chromium sandbox is the primary containment boundary between
    hostile page content and the host — disabling it globally weakened local
    dev and CI runs to satisfy a container constraint. Now conditional: off
    (sandbox stays enabled) unless a container is detected or explicitly
    overridden via SCRAPER_DISABLE_SANDBOX (most container runtimes need it
    off regardless of detection, since --no-sandbox requires either a
    privileged container or this flag).
    """
    override = os.environ.get("SCRAPER_DISABLE_SANDBOX", "").strip().lower()
    if override in ("1", "true", "yes"):
        return True
    if override in ("0", "false", "no"):
        return False
    return _running_in_container()

class BrowserContextPool:
    """
    Spacescraper Browser Node.
    Owns one long-lived Chromium process and hands out isolated, persona-bound
    Playwright contexts against it — re-launching Chromium per URL is the
    overhead this avoids, not context creation, which is cheap.

    There used to be a warm queue of generic contexts here as well, leased by
    an acquire() call that passed no Fingerprint. Nothing ever made that call:
    UA/viewport/locale/timezone are new_context()-only options, so a coherent
    persona cannot be retrofitted onto an already-created context, and every
    caller therefore goes through the persona-bound path. The warm contexts
    were created at startup, replenished on a timer, and closed at shutdown
    without ever being handed to anybody (D28).

    Nothing bounds how many contexts are live at once. That is currently
    exact rather than optimistic: the only production caller is
    worker_scraper, whose stream consumer processes one entry at a time, so
    at most one context exists per process. If a consumer ever fans out, an
    asyncio.Semaphore around acquire/release is where the bound belongs —
    the queue that was deleted here never provided one either, since the
    path it guarded was the one nobody took.
    """

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright = None
        self._browser: Browser | None = None

        self._is_initialized = False
        self._lock = asyncio.Lock()

        # Metrics
        self._contexts_created = 0

        # Read once from the driven browser at startup (S1): the largest
        # single tell in the old stack was a hardcoded UA Chrome version
        # that could silently drift from the Chromium actually launched.
        self.chromium_major: int | None = None

    async def initialize(self):
        """
        Bootstraps the browser process.
        Configures stealth arguments to minimize anti-bot detection at the process level.

        Everything after the driver starts runs under a failure guard. A raise
        anywhere in here used to leave the launched Chromium running with
        _is_initialized still False, so the next acquire() — which
        auto-initializes — launched a second one on top of it, once per job
        for the life of the worker (D11).
        """
        async with self._lock:
            if self._is_initialized:
                return

            logger.info("Spacescraper: Provisioning BrowserContextPool")
            self._playwright = await async_playwright().start()

            # Industrial Evasion Arguments: Disables blink features that reveal automation.
            # --disable-gpu + --disable-software-rasterizer used to make
            # getContext('webgl') return null unconditionally — no WebGL at
            # all is itself a stronger signal than a spoofed renderer.
            # --use-gl=swiftshader gives headless software GL instead.
            browser_args = [
                '--disable-blink-features=AutomationControlled',
                '--disable-infobars',
                '--disable-dev-shm-usage',  # Resilience for containerized environments
                '--use-gl=swiftshader',
                '--disable-background-networking',
                '--disable-background-timer-throttling',
                '--disable-renderer-backgrounding',
                # A repeated --disable-features switch does not merge with
                # Chromium's own arg parsing — only the last occurrence is
                # honoured, so the old two-line form silently dropped
                # TranslateUI. One line, one set.
                '--disable-features=TranslateUI,IsolateOrigins',
                '--disable-site-isolation-trials',
                # A1: headless-detection bypass — a maximized real window
                # avoids the small/zero outerWidth-outerHeight tell.
                '--start-maximized',
                # A1: headless Chromium otherwise reports touch-capable
                # pointer/hover types; force desktop mouse semantics.
                # PointerType: none=1 coarse=2 fine=4; HoverType: none=1 hover=2.
                '--blink-settings=primaryHoverType=2,availableHoverTypes=2,'
                'primaryPointerType=4,availablePointerTypes=4',
                # A1: fingerprint noise via Chromium flags, not a JS patch —
                # a flag has no JS-observable override to detect.
                '--fingerprinting-canvas-image-data-noise',
                '--webrtc-ip-handling-policy=disable_non_proxied_udp',
                '--force-webrtc-ip-handling-policy',
                # A1: force DNS-over-HTTPS so the plain-DNS leak doesn't
                # contradict a proxied/VPN'd connection. Unlike the WebGL
                # flags in S1, this wasn't verified against a live launch —
                # if a target environment's Chromium build handles this
                # differently, this is the first flag to check.
                '--dns-over-https-templates=https://cloudflare-dns.com/dns-query',
                '--enable-features=DnsOverHttps',
            ]
            if _sandbox_should_be_disabled():
                browser_args.extend(['--no-sandbox', '--disable-setuid-sandbox'])
                logger.warning(
                    "Spacescraper: Chromium sandbox DISABLED (container detected or "
                    "SCRAPER_DISABLE_SANDBOX set) — see DEPLOYMENT.md for residual risk."
                )

            try:
                self._browser = await self._playwright.chromium.launch(
                    headless=self.headless,
                    args=browser_args,
                    # A1: strip Playwright's own automation tells. Playwright adds
                    # these by default; --enable-automation is what triggers the
                    # "Chrome is being controlled by automated test software" bar.
                    ignore_default_args=[
                        '--enable-automation',
                        '--disable-extensions',
                        '--disable-default-apps',
                        '--disable-component-update',
                    ],
                )
                self.chromium_major = int(self._browser.version.split(".")[0])
            except BaseException:
                await self._teardown()
                raise

            self._is_initialized = True
            logger.info(
                f"Spacescraper: Browser cluster online (driven Chromium major "
                f"{self.chromium_major})."
            )

    async def _teardown(self) -> None:
        """Close the browser and driver and forget them. Caller holds _lock.

        Best-effort throughout: this runs on the failure path of initialize()
        as well as on shutdown, and a close that raises there would replace
        the real cause with its own.
        """
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                logger.debug("Browser close failed", exc_info=True)
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                logger.debug("Playwright stop failed", exc_info=True)
            self._playwright = None

    async def _create_stealth_context(
        self, fingerprint: Fingerprint, proxy: dict | None = None,
    ) -> BrowserContext:
        """
        Constructs an isolated context bound to the given Fingerprint. Its
        user_agent/viewport/locale/timezone/device_scale_factor are applied
        as new_context() options — Playwright options, not JS overrides — so
        the resulting navigator.userAgent, HTTP User-Agent header, and
        derived client hints (Sec-CH-UA-Platform, screen) all agree by
        construction.

        webdriver is left alone: --disable-blink-features=AutomationControlled
        (a launch arg, not a JS override) already handles it, and a JS
        `defineProperty` here would make it non-configurable — which is
        exactly what made the previous page-level override throw and abort
        the rest of the init script.

        A1 also lists `ignore_https_errors=True` for MITM-proxy compatibility.
        Deliberately not added here: applied globally it disables TLS
        certificate validation for every scrape target, the same blanket
        weakening SEC-4 removed `bypass_csp` for. If a proxy tier needs it,
        it belongs on that job's context specifically, with the reason
        logged — not silently on by default.
        """
        context = await self._browser.new_context(
            viewport={"width": fingerprint.viewport[0], "height": fingerprint.viewport[1]},
            java_script_enabled=True,
            user_agent=fingerprint.user_agent,
            locale=fingerprint.locale,
            timezone_id=fingerprint.timezone,
            device_scale_factor=fingerprint.device_scale_factor,
            is_mobile=False,
            has_touch=fingerprint.has_touch,
            # A1: defeats the prefersLightColor heuristic some anti-bot
            # scripts use against headless Chromium's light-only default.
            color_scheme="dark",
            proxy=proxy,
        )
        try:
            await context.add_init_script(f"""
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {{
                    if (parameter === 37445) return {fingerprint.vendor!r};
                    if (parameter === 37446) return {fingerprint.renderer!r};
                    return getParameter.apply(this, arguments);
                }};
                const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
                WebGL2RenderingContext.prototype.getParameter = function(parameter) {{
                    if (parameter === 37445) return {fingerprint.vendor!r};
                    if (parameter === 37446) return {fingerprint.renderer!r};
                    return getParameter2.apply(this, arguments);
                }};
            """)
        except BaseException:
            # new_context() already created this in the browser process. If the
            # raise propagates as-is the only reference to it dies with this
            # frame, so nothing can ever close it and it holds its share of
            # Chromium's memory until the process exits (D10). Suppressed
            # rather than chained: a close failure here must not replace the
            # real cause.
            with contextlib.suppress(Exception):
                await context.close()
            raise
        return context

    async def acquire(
        self, fingerprint: Fingerprint, proxy: dict | None = None,
    ) -> BrowserContext:
        """
        Leases a fresh, persona-bound context.
        UA/viewport/locale/timezone are new_context()-only options, so a
        coherent persona cannot be retrofitted onto an already-created
        context — which is why there is no shared context to hand out and a
        Fingerprint is required. proxy (P3), Playwright's
        {"server": ..., "username": ..., "password": ...} shape, is likewise
        new_context()-only.
        Auto-initializes if not already initialized.
        """
        if not self._is_initialized:
            await self.initialize()

        context = await self._create_stealth_context(fingerprint, proxy=proxy)
        self._contexts_created += 1
        logger.debug("Spacescraper: Lease granted for persona-bound browser context.")
        return context

    async def release(self, context: BrowserContext):
        """
        Ends a lease. Every context is bound to one persona, so it is closed
        rather than cleaned and reused — handing a persona-A context to
        persona-B would reintroduce exactly the UA/viewport mismatch S1
        exists to remove.
        """
        try:
            await context.close()
        except Exception:
            logger.debug("Context close failed during lease return", exc_info=True)

    async def get_metrics(self) -> dict:
        """Get pool metrics for monitoring."""
        return {
            "contexts_created": self._contexts_created,
            "initialized": self._is_initialized,
        }

    async def close_all(self):
        """
        Graceful cluster shutdown.
        Terminates the underlying Chromium engine. Live contexts go down with
        it — each one belongs to whichever lease still holds it, and release()
        is that lease's job.
        """
        logger.info("Spacescraper: Shutting down browser cluster...")

        async with self._lock:
            self._is_initialized = False
            await self._teardown()
            logger.info("Spacescraper: Browser cluster shutdown complete.")
