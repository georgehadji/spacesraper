# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Crawling Engine)
# Role: High-level orchestration of browser automation, network interception, and anti-bot evasion.

import json
import logging
import os
from datetime import datetime
from typing import Any

from playwright.async_api import BrowserContext, Page, Route

from src.domain.block_signal import detect_block
from src.domain.fingerprint import Fingerprint
from src.domain.models import RawScrapePayload
from src.domain.throttle import parse_retry_after
from src.infrastructure.browser.persona import persona_manager
from src.infrastructure.browser.pool import BrowserContextPool
from src.infrastructure.monitoring.observability import metrics_tracker

# Localized logger for detailed transaction logs
logger = logging.getLogger("Spacescraper.Engine")

# SEC-3: intercepted JSON responses were buffered with no size cap, no count
# cap, and no total-bytes cap — a page emitting large or numerous JSON
# responses drove worker memory without bound. All three are configurable.
INTERCEPT_MAX_RESPONSE_BYTES = int(os.environ.get("INTERCEPT_MAX_RESPONSE_BYTES", 2_000_000))
INTERCEPT_MAX_COUNT = int(os.environ.get("INTERCEPT_MAX_COUNT", 200))
INTERCEPT_MAX_TOTAL_BYTES = int(os.environ.get("INTERCEPT_MAX_TOTAL_BYTES", 20_000_000))


def _is_json_content_type(content_type: str) -> bool:
    """Widened match: application/json, application/ld+json, text/json,
    application/vnd.api+json, etc. — anything whose media type ends in json."""
    media_type = content_type.split(";", 1)[0].strip()
    return media_type.endswith("json")


def _forensic_screenshots_enabled() -> bool:
    """
    SEC-6: forensic screenshots can contain personal data from the target
    page — the codebase redacts PII before sending text to an LLM but
    applies nothing here — and accumulate unbounded in exports/evidence/.
    Off by default; opt in to debug a specific failure.
    """
    return os.environ.get("SCRAPER_FORENSIC_SCREENSHOTS", "false").strip().lower() in ("1", "true", "yes")


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
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        
        # Buffer for background data capture
        self.intercepted_json: list[dict[str, Any]] = []
        self._intercept_bytes_total = 0
        self._intercept_overflow_count = 0
        self._interception_errors = 0
        self.persona: Fingerprint | None = None

    async def start(self, persona_id: str | None = None, proxy: dict | None = None):
        """
        Initializes a browser session with a coherent Fingerprint.
        UA, viewport, locale, timezone, device_scale_factor, and the WebGL
        vendor/renderer override are all applied at context creation (S1) —
        so navigator.userAgent, the HTTP User-Agent header, and the derived
        client hints agree by construction. Nothing is patched per page.
        proxy (P3) is Playwright's {"server": ...} shape, from a SessionPool
        lease — bound to the same session as persona_id, never rotated alone.
        """
        logger.info(f"Spacescraper Engine: Acquiring browser lease [Persona: {persona_id or 'Anonymous'}]")
        chromium_major = self.context_pool.chromium_major or 120
        self.persona = persona_manager.generate_fingerprint(persona_id, chromium_major)
        self.context = await self.context_pool.acquire(fingerprint=self.persona, proxy=proxy)

        self.page = await self.context.new_page()

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
        Captures JSON payloads flying over the wire (endpoint URL, status,
        content-type, and body) — this is what turbo mode replays directly
        over HTTP on a domain's next job, skipping the browser entirely.
        Bounded by per-response, per-page-count, and per-page-total-bytes
        caps (SEC-3): a page emitting large or numerous JSON responses must
        not drive worker memory without bound.
        """
        try:
            content_type = response.headers.get("content-type", "").lower()
            if not _is_json_content_type(content_type) or not response.ok:
                return

            if len(self.intercepted_json) >= INTERCEPT_MAX_COUNT:
                self._intercept_overflow_count += 1
                logger.debug(
                    f"Spacescraper Intercept: per-page count cap ({INTERCEPT_MAX_COUNT}) "
                    f"reached, dropping {response.url}"
                )
                return

            body = await response.body()
            if len(body) > INTERCEPT_MAX_RESPONSE_BYTES:
                self._intercept_overflow_count += 1
                logger.debug(
                    f"Spacescraper Intercept: response from {response.url} exceeds "
                    f"per-response cap ({len(body)} > {INTERCEPT_MAX_RESPONSE_BYTES} bytes), skipped"
                )
                return
            if self._intercept_bytes_total + len(body) > INTERCEPT_MAX_TOTAL_BYTES:
                self._intercept_overflow_count += 1
                logger.debug(
                    f"Spacescraper Intercept: per-page total-bytes cap "
                    f"({INTERCEPT_MAX_TOTAL_BYTES}) reached, dropping {response.url}"
                )
                return

            data = json.loads(body)
            self._intercept_bytes_total += len(body)
            logger.debug(f"Spacescraper Intercept: Captured JSON from {response.url}")
            self.intercepted_json.append({
                "url": response.url,
                "status": response.status,
                "content_type": content_type,
                "data": data,
            })
        except Exception:
            self._interception_errors += 1
            logger.debug(
                f"Spacescraper Intercept: failed to capture response from "
                f"{getattr(response, 'url', '?')}", exc_info=True
            )

    async def _detect_and_handle_captcha(self, status_code: int | None = None) -> bool:
        """
        Forensic Anti-Bot Detection (A1's BlockSignalDetector, shared with the
        turbo/httpx tier). Status + challenge title + Turnstile/managed-
        challenge body markers — the content-length-collapse signal needs a
        persisted rolling median and isn't implemented yet (see block_signal.py).
        """
        title = await self.page.title()
        body_sample = ""
        try:
            body_sample = (await self.page.content())[:4000]
        except Exception:
            logger.debug("Could not sample page body for block detection", exc_info=True)

        signal = detect_block(status_code=status_code, title=title, body_sample=body_sample)
        if signal.blocked:
            logger.warning(f"Spacescraper ALERT: Challenge detected on {self.page.url} ({signal.reason})")
            await metrics_tracker.increment("captcha_encountered")
            # Logic for integrating 2Captcha/CapMonster would be triggered here
            return True
        return False

    async def crawl(
        self, url: str, network_idle: bool = False, wait_selector: str | None = None
    ) -> RawScrapePayload:
        """
        Synchronous navigation and data capture.
        Returns a RawScrapePayload wrapping the captured DOM and JSON traffic.
        """
        self.intercepted_json = []
        self._intercept_bytes_total = 0
        self._intercept_overflow_count = 0
        self._interception_errors = 0
        payload = RawScrapePayload(
            job_id="CLUSTER_ID_PENDING",
            target_site="STRATEGY_PENDING",
            url=url,
            status_code=0
        )

        try:
            logger.info(f"Spacescraper: Navigating to {url}")
            # 'load' is the default: pages with polling, ads, analytics beacons,
            # or a websocket never reach network idle and used to burn the full
            # timeout on every single fetch. network_idle/wait_selector are opt
            # in per-job, and a timeout on either is a shrug, not a failure —
            # the page is usually already usable.
            response = await self.page.goto(url, wait_until="load", timeout=self.timeout)

            if wait_selector:
                try:
                    await self.page.wait_for_selector(wait_selector, timeout=self.timeout)
                except Exception:
                    logger.debug(
                        f"wait_selector {wait_selector!r} did not appear before timeout", exc_info=True
                    )
            elif network_idle:
                try:
                    await self.page.wait_for_load_state("networkidle", timeout=self.timeout)
                except Exception:
                    logger.debug("networkidle wait timed out; continuing with current page state", exc_info=True)

            # Scenario 3: Visual Regression & Forensic Screenshots
            if not response or not response.ok:
                await self._capture_forensic_screenshot(url, "navigation_failure")
            
            if response:
                payload.status_code = response.status
                if response.status in (429, 503):
                    retry_after = response.headers.get("retry-after")
                    if retry_after:
                        payload.retry_after_s = parse_retry_after(retry_after)

            # Post-load audit: Did we hit a wall?
            has_captcha = await self._detect_and_handle_captcha(status_code=payload.status_code)
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
        Gated by SCRAPER_FORENSIC_SCREENSHOTS (SEC-6) — see that flag's
        docstring for why this isn't unconditional.
        """
        if not _forensic_screenshots_enabled():
            return
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
            except Exception:
                logger.debug("Page close failed during lease return", exc_info=True)
        if self.context:
            # Return context to pool for next job
            await self.context_pool.release(self.context)
