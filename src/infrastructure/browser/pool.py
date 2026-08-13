# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Browser Orchestration)
# Role: Manages a high-performance pool of isolated Playwright browser contexts.

import asyncio
import logging

from playwright.async_api import Browser, BrowserContext, async_playwright

# Initialize localized logger for browser cluster telemetry
logger = logging.getLogger("Spacescraper.BrowserPool")

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
            browser_args = [
                '--disable-blink-features=AutomationControlled',
                '--disable-infobars',
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',  # Resilience for containerized environments
                '--disable-gpu',  # Reduce resource usage
                '--disable-software-rasterizer',
                '--disable-background-networking',
                '--disable-background-timer-throttling',
                '--disable-renderer-backgrounding',
                '--disable-features=TranslateUI',  # Disable translation popup
                '--disable-features=IsolateOrigins',
                '--disable-site-isolation-trials',
            ]
            
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=browser_args
            )
            
            # Pre-fill the queue with reusable contexts
            for _ in range(self.pool_size):
                context = await self._create_stealth_context()
                await self._context_queue.put(context)
                self._contexts_created += 1
                
            self._is_initialized = True
            logger.info("Spacescraper: Browser cluster is online and warm.")
            
            # Start health check task
            self._health_check_task = asyncio.create_task(self._health_check_loop())

    async def _create_stealth_context(self) -> BrowserContext:
        """
        Constructs an isolated stealth-enhanced context.
        Injects fingerprints and overrides navigator properties to bypass basic WAFs.
        """
        context = await self._browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            java_script_enabled=True,
            bypass_csp=True,  # Allows for deeper script interrogation
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        # Anti-Detection Injection: Nullifies the 'webdriver' flag in the global context
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            // Hide automation indicators
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 4 });
        """)
        return context

    async def acquire(self) -> BrowserContext:
        """
        Leases a context from the pool. 
        Blocks the caller if the cluster is at maximum capacity.
        Auto-initializes if not already initialized.
        """
        if not self._is_initialized:
            await self.initialize()
            
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
                    pass  # Non-critical
                self._contexts_recycled += 1
                
            await self._context_queue.put(context)
            logger.debug("Spacescraper: Browser context returned to cluster.")
            
        except Exception as e:
            # Recovery: If the context is corrupted, kill it and provision a fresh replacement
            logger.error(f"Spacescraper Context Fault: {e}. Provisioning replacement...")
            try:
                await context.close()
            except Exception:
                pass
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
                        pass
                except asyncio.QueueEmpty:
                    break
                
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                    
            logger.info("Spacescraper: Browser cluster shutdown complete.")
