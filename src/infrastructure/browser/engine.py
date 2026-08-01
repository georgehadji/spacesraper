# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Crawling Engine)
# Role: High-level orchestration of browser automation, network interception, and anti-bot evasion.

import logging
import json
import os
from datetime import datetime
from typing import Optional, Dict, Any, List
from src.domain.models import RawScrapePayload
from playwright.async_api import Page, BrowserContext, Route
from src.infrastructure.browser.pool import BrowserContextPool
from src.infrastructure.browser.persona import persona_manager
from src.infrastructure.monitoring.observability import metrics_tracker

# Localized logger for detailed transaction logs
logger = logging.getLogger("Spacescraper.Engine")

class ScraperEngine:
    """
    Spacescraper Performance Node.
    This class wraps the Playwright API to provide specialized scraping features:
    1. Background Network Interception (captures JSON/XHR data).
    2. Intelligent Resource Blocking (saves bandwidth/CPU by skipping CSS/Media).
    3. Anti-Bot Detection (heuristics for CAPTCHAs and WAF walls).
    """
    
    def __init__(self, context_pool: BrowserContextPool, timeout: int = 35000):
        self.context_pool = context_pool
        self.timeout = timeout
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        
        # Buffer for background data capture
        self.intercepted_json: List[Dict[str, Any]] = []
        self.persona: Optional[Dict[str, Any]] = None

    async def start(self, persona_id: Optional[str] = None):
        """
        Initializes a browser session with a unique Shadow Persona.
        """
        logger.info(f"Spacescraper Engine: Acquiring browser lease [Persona: {persona_id or 'Anonymous'}]")
        self.context = await self.context_pool.acquire()
        
        # Scenario 4: Dynamic Morphing
        self.persona = persona_manager.generate_persona(persona_id)
        
        # Open a new page, then morph it to the persona.
        # user_agent and viewport are new_context() options, not new_page() ones:
        # passing them here raises TypeError and fails every browser scrape. The
        # contexts are pooled and reused, so the persona is applied per page —
        # viewport directly, user agent through the request header plus the
        # navigator override injected below.
        self.page = await self.context.new_page()

        browser_config = self.persona["browser_config"]
        user_agent = browser_config["user_agent"]
        await self.page.set_viewport_size(browser_config["viewport"])
        await self.page.set_extra_http_headers({"User-Agent": user_agent})

        # Inject evasion script into all frames
        evasion = self.persona["evasion_scripts"]
        await self.page.add_init_script(f"""
            Object.defineProperty(navigator, 'webdriver', {{get: () => false}});
            Object.defineProperty(navigator, 'userAgent', {{get: () => {json.dumps(user_agent)}}});
            // WebGL Morpher
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {{
                if (parameter === 37445) return '{evasion["webgl_vendor"]}';
                if (parameter === 37446) return '{evasion["webgl_renderer"]}';
                return getParameter.apply(this, arguments);
            }};
        """)
        
        # Setup background listeners for asynchronous data capture (XHR/Fetch)
        self.page.on("response", self._intercept_response)
        
        # Apply Enterprise Routing: Block heavy assets to maximize throughput
        await self.page.route("**/*", self._route_interceptor)

    async def _route_interceptor(self, route: Route):
        """
        Bandwidth Optimization Logic.
        Aborts requests for images, fonts, and trackers. This significantly 
        speeds up page loading while keeping the Document and JS functional.
        """
        request = route.request
        res_type = request.resource_type
        
        # Resource Blacklist: Abort if the asset doesn't contribute to data extraction
        if res_type in ["image", "media", "font", "stylesheet", "other"]:
            await route.abort()
        # Privacy & Performance: Block known analytic trackers
        elif any(domain in request.url for domain in ["google-analytics", "facebook.com/tr", "segment.io"]):
            await route.abort()
        else:
            # Allow essential scripts and the document itself
            await route.continue_()

    async def _intercept_response(self, response):
        """
        Network Intelligence Listener.
        Captures JSON payloads flying over the wire. This often allows 
        us to get cleaner data than parsing the DOM.
        """
        try:
            content_type = response.headers.get("content-type", "").lower()
            if "application/json" in content_type:
                if response.ok:
                    data = await response.json()
                    logger.debug(f"Spacescraper Intercept: Captured JSON from {response.url}")
                    self.intercepted_json.append({
                        "url": response.url,
                        "data": data
                    })
        except Exception:
            # Silently skip malformed or unreadable responses
            pass 

    async def _detect_and_handle_captcha(self) -> bool:
        """
        Forensic Anti-Bot Detection.
        Parses page metadata to identify if we have been challenged by a WAF.
        """
        title = await self.page.title()
        detectors = ["Just a moment...", "Attention Required", "Access Denied", "Checking your browser"]
        
        if any(d in title for d in detectors):
            logger.warning(f"Spacescraper ALERT: Challenge detected on {self.page.url}")
            await metrics_tracker.increment("captcha_encountered")
            # Logic for integrating 2Captcha/CapMonster would be triggered here
            return True
        return False

    async def crawl(self, url: str) -> RawScrapePayload:
        """
        Synchronous navigation and data capture.
        Returns a RawScrapePayload wrapping the captured DOM and JSON traffic.
        """
        self.intercepted_json = []
        payload = RawScrapePayload(
            job_id="CLUSTER_ID_PENDING", 
            target_site="STRATEGY_PENDING", 
            url=url, 
            status_code=0
        )
        
        try:
            logger.info(f"Spacescraper: Navigating to {url}")
            # Navigate with 'networkidle' to ensure SPAs have finished data loading
            response = await self.page.goto(url, wait_until="networkidle", timeout=self.timeout)
            
            # Scenario 3: Visual Regression & Forensic Screenshots
            if not response or not response.ok:
                await self._capture_forensic_screenshot(url, "navigation_failure")
            
            if response:
                payload.status_code = response.status
                
            # Post-load audit: Did we hit a wall?
            has_captcha = await self._detect_and_handle_captcha()
            if has_captcha:
                await self._capture_forensic_screenshot(url, "captcha_detected")
                payload.error_message = f"Challenge detected (Status: {payload.status_code}). Engine cannot bypass."
            else:
                # Capture the full rendered DOM and all intercepted network traffic
                payload.html_content = await self.page.content()
                payload.json_payloads = self.intercepted_json
            
            # Record observation metrics
            await metrics_tracker.increment("pages_scraped")
            
            
        except Exception as e:
            logger.error(f"Spacescraper Error: Persistent fault on {url}: {e}")
            await self._capture_forensic_screenshot(url, "critical_fault")
            payload.error_message = str(e)
            payload.status_code = 500
            
        return payload

    async def _capture_forensic_screenshot(self, url: str, reason: str):
        """
        Scenario 3: Forensic & Visual Audit.
        Captures a visual record of the failure for offline analysis.
        """
        try:
            os.makedirs("exports/evidence", exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"exports/evidence/{reason}_{timestamp}.png"
            await self.page.screenshot(path=filename, full_page=True)
            logger.info(f"Spacescraper Forensic: Evidence captured at {filename}")
        except Exception as e:
            logger.debug(f"Forensic capture suppressed: {e}")

    async def close(self):
        """
        Resource Release Sequence.
        Closes the browser page and returns the context to the pool for reuse.
        """
        logger.info("Spacescraper Engine: Returning browser lease to cluster.")
        if self.page:
            try:
                await self.page.close()
            except: pass
        if self.context:
            # Return context to pool for next job
            await self.context_pool.release(self.context)
