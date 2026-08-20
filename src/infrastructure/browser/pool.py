# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Browser Orchestration)
# Role: Manages a high-performance pool of isolated Playwright browser contexts.

import asyncio
import logging
import os

from playwright.async_api import Browser, BrowserContext, async_playwright

from src.domain.fingerprint import Fingerprint

# Initialize localized logger for browser cluster telemetry
logger = logging.getLogger("Spacescraper.BrowserPool")


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
    Spacescraper High-Availability Browser Node.
    To achieve industrial-scale scraping, Spacescraper maintains a 'warm' pool 
    of browser contexts. This architecture avoids the massive overhead of 
    re-launching a Chromium process for every URL, while keeping 
    individual tasks isolated and stateless via Playwright Contexts.
    """
    
    def __init__(self, pool_size: int = 5, headless: bool = True):
        self.pool_size = pool_size
        self.headless = headless
        self._playwright = None
        self._browser: Browser | None = None
        
        # Async Queue acts as a thread-safe semaphore for available browser resources
        self._context_queue: asyncio.Queue[BrowserContext] = asyncio.Queue(maxsize=pool_size)
        self._is_initialized = False
        self._lock = asyncio.Lock()
        self._health_check_interval = 300  # 5 minutes
        self._health_check_task: asyncio.Task | None = None
        
        # Metrics
        self._contexts_created = 0
        self._contexts_recycled = 0

        # Read once from the driven browser at startup (S1): the largest
        # single tell in the old stack was a hardcoded UA Chrome version
        # that could silently drift from the Chromium actually launched.
        self.chromium_major: int | None = None

        # Contexts created for a specific Fingerprint (via acquire(fingerprint=...))
        # are closed on release rather than recycled into the shared warm
        # queue — reusing a persona-A context for persona-B would reintroduce
        # exactly the UA/viewport mismatch S1 exists to remove.
        self._fingerprint_bound_ids: set[int] = set()

    async def initialize(self):
        """
        Bootstraps the browser process and provisions the context pool.
        Configures stealth arguments to minimize anti-bot detection at the process level.
        """
        async with self._lock:
            if self._is_initialized:
                return
                
            logger.info(f"Spacescraper: Provisioning BrowserContextPool (Size: {self.pool_size})")
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
            ]
            if _sandbox_should_be_disabled():
                browser_args.extend(['--no-sandbox', '--disable-setuid-sandbox'])
                logger.warning(
                    "Spacescraper: Chromium sandbox DISABLED (container detected or "
                    "SCRAPER_DISABLE_SANDBOX set) — see DEPLOYMENT.md for residual risk."
                )

            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=browser_args
            )
            self.chromium_major = int(self._browser.version.split(".")[0])
            logger.info(f"Spacescraper: Driven Chromium major version {self.chromium_major}.")

            # Pre-fill the queue with reusable, unbound contexts for callers
            # that don't need a specific persona. A persona-bound context is
            # created fresh per acquire(fingerprint=...) instead (see acquire).
            for _ in range(self.pool_size):
                context = await self._create_stealth_context()
                await self._context_queue.put(context)
                self._contexts_created += 1
                
            self._is_initialized = True
            logger.info("Spacescraper: Browser cluster is online and warm.")
            
            # Start health check task
            self._health_check_task = asyncio.create_task(self._health_check_loop())

    async def _create_stealth_context(self, fingerprint: Fingerprint | None = None) -> BrowserContext:
        """
        Constructs an isolated context. When a Fingerprint is given, its
        user_agent/viewport/locale/timezone/device_scale_factor are applied
        as new_context() options — Playwright options, not JS overrides — so
        the resulting navigator.userAgent, HTTP User-Agent header, and
        derived client hints (Sec-CH-UA-Platform, screen) all agree by
        construction. Falls back to a generic unbound context otherwise
        (used only to keep the warm pool populated).

        webdriver is left alone: --disable-blink-features=AutomationControlled
        (a launch arg, not a JS override) already handles it, and a JS
        `defineProperty` here would make it non-configurable — which is
        exactly what made the previous page-level override throw and abort
        the rest of the init script.
        """
        if fingerprint is not None:
            context = await self._browser.new_context(
                viewport={"width": fingerprint.viewport[0], "height": fingerprint.viewport[1]},
                java_script_enabled=True,
                user_agent=fingerprint.user_agent,
                locale=fingerprint.locale,
                timezone_id=fingerprint.timezone,
                device_scale_factor=fingerprint.device_scale_factor,
                is_mobile=False,
                has_touch=fingerprint.has_touch,
            )
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
            return context

        return await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            java_script_enabled=True,
        )

    async def acquire(self, fingerprint: Fingerprint | None = None) -> BrowserContext:
        """
        Leases a context from the pool.
        Passing a Fingerprint creates a fresh, persona-bound context instead
        of pulling a generic one from the warm queue — UA/viewport/locale/
        timezone are new_context()-only options, so a coherent persona
        cannot be retrofitted onto an already-created context.
        Blocks the caller if the cluster is at maximum capacity (unbound path only).
        Auto-initializes if not already initialized.
        """
        if not self._is_initialized:
            await self.initialize()

        if fingerprint is not None:
            context = await self._create_stealth_context(fingerprint)
            self._fingerprint_bound_ids.add(id(context))
            self._contexts_created += 1
            logger.debug("Spacescraper: Lease granted for persona-bound browser context.")
            return context

        context = await self._context_queue.get()
        logger.debug("Spacescraper: Lease granted for browser context.")
        return context

    async def release(self, context: BrowserContext, force_recycle: bool = False):
        """
        Returns a context to the cluster.
        Cleans state records (cookies/cache) to ensure the next task starts fresh.

        Args:
            context: The browser context to return
            force_recycle: If True, context will be closed and replaced with a fresh one
        """
        if id(context) in self._fingerprint_bound_ids:
            self._fingerprint_bound_ids.discard(id(context))
            try:
                await context.close()
            except Exception:
                logger.debug("Persona-bound context close failed", exc_info=True)
            return

        try:
            if force_recycle or self._should_recycle_context(context):
                # Context is unhealthy, replace it
                await context.close()
                context = await self._create_stealth_context()
                self._contexts_created += 1
                logger.debug("Spacescraper: Context recycled due to health check.")
            else:
                # Memory Management: Clear storage before returning to the warm pool
                await context.clear_cookies()
                # Clear local storage and session storage if possible
                try:
                    pages = context.pages
                    if pages:
                        await pages[0].evaluate("""
                            () => {
                                localStorage.clear();
                                sessionStorage.clear();
                            }
                        """)
                except Exception:
                    logger.debug("Storage clear failed during context recycle", exc_info=True)
                self._contexts_recycled += 1
                
            await self._context_queue.put(context)
            logger.debug("Spacescraper: Browser context returned to cluster.")
            
        except Exception as e:
            # Recovery: If the context is corrupted, kill it and provision a fresh replacement
            logger.error(f"Spacescraper Context Fault: {e}. Provisioning replacement...")
            try:
                await context.close()
            except Exception:
                logger.debug("Corrupted context close failed", exc_info=True)
            new_ctx = await self._create_stealth_context()
            self._contexts_created += 1
            await self._context_queue.put(new_ctx)

    def _should_recycle_context(self, context: BrowserContext) -> bool:
        """Check if a context should be recycled based on heuristics."""
        # Check if context has too many pages (memory leak indicator)
        try:
            if len(context.pages) > 1:
                return True
        except Exception:
            return True
        return False

    async def _health_check_loop(self):
        """Periodic health check to maintain pool quality."""
        while self._is_initialized:
            try:
                await asyncio.sleep(self._health_check_interval)
                
                # Check pool size and replenish if needed
                current_size = self._context_queue.qsize()
                if current_size < self.pool_size:
                    missing = self.pool_size - current_size
                    logger.info(f"Spacescraper: Replenishing {missing} missing contexts...")
                    for _ in range(missing):
                        try:
                            context = await self._create_stealth_context()
                            await self._context_queue.put(context)
                            self._contexts_created += 1
                        except Exception as e:
                            logger.error(f"Failed to create replacement context: {e}")
                            
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")

    async def get_metrics(self) -> dict:
        """Get pool metrics for monitoring."""
        return {
            "pool_size": self.pool_size,
            "available": self._context_queue.qsize() if self._is_initialized else 0,
            "contexts_created": self._contexts_created,
            "contexts_recycled": self._contexts_recycled,
            "initialized": self._is_initialized
        }

    async def close_all(self):
        """
        Graceful cluster shutdown.
        Closes all contexts and terminates the underlying Chromium engine.
        """
        # Cancel health check
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
                
        logger.info("Spacescraper: Shutting down browser cluster...")
        
        async with self._lock:
            self._is_initialized = False
            
            # Drain and close all contexts
            while not self._context_queue.empty():
                try:
                    ctx = self._context_queue.get_nowait()
                    try:
                        await ctx.close()
                    except Exception:
                        logger.debug("Context close failed during shutdown drain", exc_info=True)
                except asyncio.QueueEmpty:
                    break

            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    logger.debug("Browser close failed during shutdown", exc_info=True)
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    logger.debug("Playwright stop failed during shutdown", exc_info=True)
                    
            logger.info("Spacescraper: Browser cluster shutdown complete.")
