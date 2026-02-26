# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Advanced Crawler Worker)
# Role: Manages high-performance browser contexts to fetch raw web data.

import asyncio
import logging
import sys
from urllib.parse import urlparse
from src.infrastructure.queues.redis_worker import RedisQueueWorker
from src.infrastructure.browser.engine import ScraperEngine
from src.infrastructure.browser.pool import BrowserContextPool
from src.infrastructure.monitoring.observability import metrics_tracker
from src.infrastructure.browser.stealth_brain import stealth_brain
from src.domain.models import ScrapeJob, RawScrapePayload

from src.infrastructure.logger_config import setup_production_logging
from src.domain.exceptions import ScrapeFailure, StealthViolation
from src.infrastructure.http_client import http_client

logger = logging.getLogger("Spacescraper.Scraper")

class ScraperWorkerService:
    """
    Spacescraper Node: Scraper Engine.
    This service is responsible for the 'E' in ETL (Extract). It utilizes
    enterprise-grade browser context pooling (Playwright) to handle modern
    Single Page Applications (SPAs) and dynamic content effectively.
    """

    TURBO_MISS_THRESHOLD = 3  # consecutive empty yields before domain demotion

    def __init__(self):
        # Redis interface for job intake and payload distribution
        self.queue = RedisQueueWorker()
        # High-performance context pool to minimize browser startup latency
        self.context_pool = BrowserContextPool(pool_size=2)
        # Hybrid AI/API Registry (Patterns mapped to API endpoints)
        self.hybrid_registry = {}
        # Domain-based registry for more robust matching
        self.hybrid_domains = set()
        # Dead man's switch: consecutive empty-yield counts per turbo domain
        self._turbo_miss_counts: dict = {}
        
    def _get_domain(self, url: str) -> str:
        """Extract domain from URL for more robust hybrid registry matching."""
        try:
            return urlparse(url).netloc.lower()
        except Exception:
            return url.lower()
        
    async def process_job(self, job: ScrapeJob):
        """
        Executes a single scraping task. 
        Requests a context from the pool, navigates to the target URL, 
        and produces a RawScrapePayload for the processor as output.
        """
        logger.info(f"Spacescraper Activity: Processing {job.job_id} [Depth: {job.depth}] -> {job.url}")
        
        domain = self._get_domain(job.url)
        
        # Zero-Browser Turbo Mode (Hybrid API Emulation Check)
        if job.url in self.hybrid_registry or domain in self.hybrid_domains:
            logger.info(f"Spacescraper Turbo: Site {domain} supports Direct API extraction. Bypassing Browser.")
            try:
                raw_payload = await self._perform_turbo_scrape(job)

                if not raw_payload.json_payloads:
                    # Semantic failure: transport succeeded but no intelligence returned
                    miss_count = self._turbo_miss_counts.get(domain, 0) + 1
                    self._turbo_miss_counts[domain] = miss_count
                    if miss_count >= self.TURBO_MISS_THRESHOLD:
                        logger.warning(
                            f"Spacescraper: Turbo yield failure for {domain} "
                            f"({miss_count} consecutive empty responses). Demoting to browser mode."
                        )
                        self.hybrid_registry.pop(job.url, None)
                        self.hybrid_domains.discard(domain)
                        self._turbo_miss_counts.pop(domain, None)
                        await metrics_tracker.increment("turbo_yield_failure")
                    # Record as failure and do not forward empty payload downstream
                    await metrics_tracker.record_job_status(success=False)
                    return  # Browser fallback will handle next attempt for this domain
                else:
                    # Successful yield — reset miss counter, forward downstream
                    self._turbo_miss_counts.pop(domain, None)
                    await metrics_tracker.record_job_status(success=True)
                    await self.queue.push_raw_payload("raw_data_queue", raw_payload)
                    return
            except Exception as e:
                logger.warning(f"Spacescraper Turbo Fault: Falling back to Browser context. Error: {e}")

        # Instantiate the scraper engine tied to our context pool
        engine = ScraperEngine(context_pool=self.context_pool)
        
        try:
            # Start the engine logic with persistent Shadow Persona
            await engine.start(persona_id=job.persona_id)
            
            # Perform the actual crawl and data capture (HTML + JSON XHR)
            raw_payload = await engine.crawl(job.url)
            
            # Learn for future missions: If clean JSON was captured, promote to Hybrid
            if raw_payload.json_payloads and not raw_payload.error_message:
                logger.debug(f"Spacescraper Intelligence: Promoting {domain} to Hybrid Engine (API Found).")
                self.hybrid_registry[job.url] = True
                self.hybrid_domains.add(domain)
            
            # Map system metadata back to the result payload
            raw_payload.job_id = job.job_id
            raw_payload.target_site = job.target_site
            raw_payload.depth = job.depth
            raw_payload.overlay = job.overlay
            raw_payload.webhook_url = job.webhook_url
            
            if raw_payload.error_message:
                # Autonomous Circuit Breaking (Stealth Violation)
                if "challenge detected" in raw_payload.error_message.lower():
                    raise StealthViolation(raw_payload.error_message, code="STEALTH_BREACHED")
                else:
                    raise ScrapeFailure(raw_payload.error_message, code="FETCH_FAILED")
            else:
                # Report success and push data to the Processing Queue
                await metrics_tracker.record_job_status(success=True)
                
                # Evolutionary Success Recording
                if engine.persona:
                    await stealth_brain.register_success(engine.persona)
                    
                await self.queue.push_raw_payload("raw_data_queue", raw_payload)
                logger.info(f"Spacescraper Success: Payload generated for {job.job_id}")
            
        except StealthViolation as e:
            logger.warning(f"Spacescraper: Stealth decay detected on {job.url}. Reporting breach.")
            await metrics_tracker.increment("stealth_decay_events")
            await metrics_tracker.record_job_status(success=False)
            await self.queue.push_dead_letter("jobs_queue", job, reason=str(e))
            
        except ScrapeFailure as e:
            logger.error(f"Spacescraper Job {job.job_id} failed: {e.message if hasattr(e, 'message') else str(e)}")
            await metrics_tracker.record_job_status(success=False)
            await self.queue.push_dead_letter("jobs_queue", job, reason=str(e))

        except Exception as e:
            # Handle unmanaged exceptions in the crawling logic
            logger.exception(f"Spacescraper Critical Flow Fault on {job.job_id}: {e}")
            await metrics_tracker.record_job_status(success=False)
            await self.queue.push_dead_letter("jobs_queue", job, reason=str(e))
            
        finally:
            # Consistently release engine resources
            await engine.close()

    async def _perform_turbo_scrape(self, job: ScrapeJob) -> RawScrapePayload:
        """
        Low-Latency API Fallback.
        Fetches data via pure HTTP requests when the site structure allows.
        """
        try:
            # Emulate the intercepted network request pattern
            response = await http_client.get(job.url)
            
            # Parse JSON response if content-type indicates JSON
            json_payloads = []
            content_type = response.headers.get("content-type", "").lower()
            if "json" in content_type:
                try:
                    json_payloads = [{"url": job.url, "data": response.json()}]
                except Exception:
                    pass
            
            payload = RawScrapePayload(
                job_id=job.job_id,
                target_site=job.target_site,
                url=job.url,
                status_code=response.status_code,
                html_content="",  # Turbo mode focuses on JSON
                json_payloads=json_payloads
            )
            await metrics_tracker.increment("turbo_mode_hits")
            return payload
            
        except Exception as e:
            logger.warning(f"Spacescraper Turbo Fault: Error: {e}")
            raise  # Re-raise to trigger fallback to browser mode

    async def run(self):
        """
        Main bootstrap method for the scraper node. 
        Warms up the pool and starts the long-polling loop against Redis.
        """
        logger.info("🚀 Spacescraper Scraper Node initializing...")
        
        # Initialize metrics tracker
        await metrics_tracker.initialize()
        
        # Pre-warm the context pool to ensure immediate readiness for incoming tasks
        await self.context_pool.initialize()

        logger.info("Spacescraper linked to Redis. Polling 'jobs_queue'...")
        
        # Try to connect to Redis (falls back to fakeredis if unavailable)
        await self.queue.connect()
        
        try:
            # Block and wait for jobs published by the Dashboard or Scheduler
            await self.queue.poll_jobs("jobs_queue", self.process_job)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            logger.info("Spacescraper Node: Graceful shutdown sequence triggered.")
        finally:
            # Resource cleanup
            pool_metrics = await self.context_pool.get_metrics()
            tracker_metrics = await metrics_tracker.get_metrics()
            logger.info(f"Spacescraper Session Stats: {tracker_metrics}")
            logger.info(f"Spacescraper Pool Stats: {pool_metrics}")
            await self.context_pool.close_all()
            await metrics_tracker.close()
            await self.queue.close()
            await http_client.close()

if __name__ == "__main__":
    # Start the worker event loop
    worker = ScraperWorkerService()
    try:
        asyncio.run(worker.run())
    except KeyboardInterrupt:
        pass
