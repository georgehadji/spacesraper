# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Advanced Crawler Worker)
# Role: Manages high-performance browser contexts to fetch raw web data.
#        Now integrates with the durable Job state machine.

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timezone
from urllib.parse import urlparse

from src.domain.exceptions import ScrapeFailure, StealthViolation
from src.domain.models import (
    Job,
    JobAttempt,
    JobState,
    MessageType,
    QueueMessage,
    RawScrapePayload,
    ScrapeJob,
    StrategyObservation,
)
from src.infrastructure.artifact_store import LocalArtifactStore
from src.infrastructure.browser.engine import ScraperEngine
from src.infrastructure.browser.pool import BrowserContextPool
from src.infrastructure.browser.stealth_brain import stealth_brain
from src.infrastructure.http_client import internal_http, target_http
from src.infrastructure.logger_config import setup_production_logging
from src.infrastructure.middleware.correlation import set_request_id
from src.infrastructure.monitoring.observability import metrics_tracker
from src.infrastructure.queues.stream_queue import ValkeyStreamQueue, make_message
from src.infrastructure.rate_limiter import DomainRateLimiter
from src.infrastructure.repositories.job_repository import SqliteJobRepository
from src.infrastructure.repositories.observation_repository import SqliteObservationRepository
from src.smart_crawler import update_url_cache

logger = logging.getLogger("Spacescraper.Scraper")

class ScraperWorkerService:
    """
    Spacescraper Node: Scraper Engine.
    This service is responsible for the 'E' in ETL (Extract). It utilizes
    enterprise-grade browser context pooling (Playwright) to handle modern
    Single Page Applications (SPAs) and dynamic content effectively.
    Integrates with the durable Job state machine for lifecycle tracking.
    """

    TURBO_MISS_THRESHOLD = 3  # consecutive empty yields before domain demotion

    def __init__(
        self,
        job_repo: SqliteJobRepository = None,
        stream_queue: ValkeyStreamQueue = None,
        obs_repo: SqliteObservationRepository = None,
    ):
        # Valkey Streams interface for job intake and payload distribution.
        # An injected queue is owned by the caller; a self-created one is closed here.
        # This matters offline: each fallback client owns a private in-memory store,
        # so scraper and processor must be handed the same instance to see each other.
        self._owns_stream_queue = stream_queue is None
        self.stream_queue = stream_queue or ValkeyStreamQueue()
        # High-performance context pool to minimize browser startup latency
        self.context_pool = BrowserContextPool(pool_size=2)
        # Job state repository for durable lifecycle tracking
        self.job_repo = job_repo or SqliteJobRepository()
        # Turbo registry: domain -> discovered API endpoints (never the page
        # URL itself). Populated when a browser fetch intercepts JSON XHR
        # traffic; replayed directly over HTTP on the next job for that
        # domain, skipping the browser entirely.
        self.domain_endpoints: dict[str, list[dict]] = {}
        # Dead man's switch: consecutive empty-yield counts per turbo domain
        self._turbo_miss_counts: dict = {}
        self.artifact_store = LocalArtifactStore()
        self.rate_limiter = DomainRateLimiter(default_budget=2)
        self.obs_repo = obs_repo or SqliteObservationRepository()

    async def _update_job_state(self, job: ScrapeJob, new_state: JobState, error_message: str = None):
        """
        Update job state in the durable repository with optimistic concurrency.

        ScrapeJob is the queue envelope and carries no version, so the current
        version is read from the durable record immediately before the update.
        A conflicting write is retried once against the refreshed version.
        """
        try:
            for _ in range(2):
                current = await self.job_repo.get_job(job.job_id)
                if current is None:
                    logger.debug("No durable job record for %s; skipping state update.", job.job_id)
                    return
                updated = await self.job_repo.update_job_state(
                    job.job_id, new_state,
                    expected_version=current.version,
                    error_message=error_message,
                )
                if updated is not None:
                    return
            logger.warning(
                "Job state update for %s lost optimistic-concurrency race twice; giving up.", job.job_id
            )
        except Exception as e:
            logger.warning(f"Failed to update job state for {job.job_id}: {e}")

    async def _create_attempt(self, job_id: str, worker_id: str = None) -> str:
        """Create a JobAttempt and return its ID."""
        attempt = JobAttempt(
            attempt_id=f"att_{uuid.uuid4().hex[:12]}",
            job_id=job_id,
            worker_id=worker_id or "scraper-unknown",
        )
        try:
            await self.job_repo.create_attempt(attempt)
        except Exception as e:
            logger.warning(f"Failed to create attempt for {job_id}: {e}")
        return attempt.attempt_id

    async def _complete_attempt(self, attempt_id: str, state: JobState, error_message: str = None):
        """Mark a JobAttempt as completed."""
        try:
            await self.job_repo.update_attempt(
                attempt_id,
                state=state,
                finished_at=datetime.now(tz=UTC).isoformat(),
                error_message=error_message,
            )
        except Exception as e:
            logger.debug(f"Failed to update attempt {attempt_id}: {e}")

    def _job_message(self, job: ScrapeJob, retry_count: int = 0) -> QueueMessage:
        """Wrap a ScrapeJob in its Streams envelope (for DLQ pushes)."""
        return make_message(
            MessageType.SCRAPE_JOB,
            job.model_dump(mode="json"),
            correlation_id=job.correlation_id,
            root_job_id=job.job_id,
            retry_count=retry_count,
        )

    def _get_domain(self, url: str) -> str:
        """Extract domain from URL for more robust hybrid registry matching."""
        try:
            return urlparse(url).netloc.lower()
        except Exception:
            return url.lower()

    async def process_job(self, job: ScrapeJob):
        """
        Executes a single scraping task under a per-domain concurrency slot.

        Every exit path — including the Turbo Mode early returns — releases the
        slot, otherwise the domain's budget would drain permanently.
        """
        domain = self._get_domain(job.url)
        slot_acquired = await self.rate_limiter.wait_for_slot(domain, timeout=60.0)
        if not slot_acquired:
            logger.warning("Rate limit timeout for domain %s, processing anyway", domain)
        try:
            await self._process_job(job, domain)
        finally:
            if slot_acquired:
                self.rate_limiter.release(domain)

    async def _heartbeat(self, job_id: str):
        """Signal the worker is alive. Never fatal: a missing record must not kill the job."""
        try:
            await self.job_repo.heartbeat(job_id)
        except Exception as e:
            logger.debug("Heartbeat failed for %s: %s", job_id, e)

    async def _process_job(self, job: ScrapeJob, domain: str):
        """
        Executes a single scraping task.
        Updates the durable Job state machine throughout the lifecycle.
        """
        logger.info(f"Spacescraper Activity: Processing {job.job_id} [Depth: {job.depth}] -> {job.url}")

        # Propagate correlation ID for end-to-end tracing
        if job.correlation_id:
            set_request_id(job.correlation_id)

        # Set job as RUNNING and create an attempt
        await self._update_job_state(job, JobState.RUNNING)
        attempt_id = await self._create_attempt(job.job_id)

        # Signal worker is alive
        await self._heartbeat(job.job_id)

        # Turbo Mode: replay previously discovered API endpoints for this
        # domain, never the page itself — the page returns HTML, its XHR
        # calls return the JSON turbo mode exists to shortcut. A miss costs
        # latency (fall through to the browser below, same job), not a
        # failed job.
        domain_endpoints = self.domain_endpoints.get(domain)
        if domain_endpoints:
            try:
                turbo_payload = await self._perform_turbo_scrape(job, domain_endpoints)
            except Exception as e:
                logger.warning(f"Spacescraper Turbo Fault: Falling back to Browser context. Error: {e}")
                turbo_payload = None

            if turbo_payload is not None and turbo_payload.json_payloads:
                self._turbo_miss_counts.pop(domain, None)
                await metrics_tracker.increment("turbo_endpoint_hit")
                await metrics_tracker.record_job_status(success=True)
                await self.stream_queue.push(
                    "raw_data_stream",
                    make_message(
                        MessageType.RAW_PAYLOAD,
                        turbo_payload.model_dump(mode="json"),
                        correlation_id=turbo_payload.correlation_id,
                        root_job_id=job.job_id,
                    ),
                )
                await self._update_job_state(job, JobState.SUCCEEDED)
                await self._complete_attempt(attempt_id, JobState.SUCCEEDED)
                await update_url_cache(job.url, json.dumps(turbo_payload.json_payloads), None)
                return
            else:
                await metrics_tracker.increment("turbo_endpoint_miss")
                miss_count = self._turbo_miss_counts.get(domain, 0) + 1
                self._turbo_miss_counts[domain] = miss_count
                if miss_count >= self.TURBO_MISS_THRESHOLD:
                    logger.warning(
                        f"Spacescraper: Turbo endpoint replay failed for {domain} "
                        f"({miss_count} consecutive misses). Demoting to browser mode."
                    )
                    self.domain_endpoints.pop(domain, None)
                    self._turbo_miss_counts.pop(domain, None)
                    await metrics_tracker.increment("turbo_yield_failure")
                # Fall through to the browser fetch below within this same job.

        # Instantiate the scraper engine tied to our context pool
        engine = ScraperEngine(context_pool=self.context_pool)

        try:
            # Start the engine logic with persistent Shadow Persona
            await engine.start(persona_id=job.persona_id)

            # Perform the actual crawl and data capture (HTML + JSON XHR)
            raw_payload = await engine.crawl(
                job.url, network_idle=job.network_idle, wait_selector=job.wait_selector
            )

            # Learn for future missions: promote the discovered API endpoints
            # (never the page URL) so the next job for this domain can skip
            # the browser via turbo replay.
            if raw_payload.json_payloads and not raw_payload.error_message:
                endpoints = [
                    {"url": p["url"], "content_type": p.get("content_type", "")}
                    for p in raw_payload.json_payloads
                    if p.get("url") and p["url"] != job.url
                ]
                if endpoints:
                    logger.debug(
                        f"Spacescraper Intelligence: Promoting {domain} to Turbo "
                        f"({len(endpoints)} endpoint(s) found)."
                    )
                    self.domain_endpoints[domain] = endpoints
                    self._turbo_miss_counts.pop(domain, None)

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

                await self.stream_queue.push(
                    "raw_data_stream",
                    make_message(
                        MessageType.RAW_PAYLOAD,
                        raw_payload.model_dump(mode="json"),
                        correlation_id=raw_payload.correlation_id,
                        root_job_id=job.job_id,
                    ),
                )
                logger.info(f"Spacescraper Success: Payload generated for {job.job_id}")

                # Store raw HTML as artifact
                if raw_payload.html_content:
                    await self.artifact_store.store(
                        raw_payload.html_content.encode("utf-8"),
                        job.url, "text/html", job_id=job.job_id,
                    )

                # Update durable job state
                await self._update_job_state(job, JobState.SUCCEEDED)
                await self._complete_attempt(attempt_id, JobState.SUCCEEDED)

                # Update cache after successful fetch
                if raw_payload.html_content:
                    await update_url_cache(job.url, raw_payload.html_content, None)
                elif raw_payload.json_payloads:
                    await update_url_cache(job.url, json.dumps(raw_payload.json_payloads), None)

                # Record strategy observation
                try:
                    obs = StrategyObservation(
                        observation_id=f"obs_{uuid.uuid4().hex[:12]}",
                        job_id=job.job_id,
                        domain=domain,
                        strategy="browser",
                        valid_record_count=1,
                        required_field_completeness=1.0,
                        success=True,
                        latency_ms=0.0,
                    )
                    await self.obs_repo.create_observation(obs)
                except Exception as e:
                    logger.debug("Failed to record observation: %s", e)

        except StealthViolation as e:
            logger.warning(f"Spacescraper: Stealth decay detected on {job.url}. Reporting breach.")
            await metrics_tracker.increment("stealth_decay_events")
            await metrics_tracker.record_job_status(success=False)
            await self.stream_queue.push_dlq("jobs_stream", self._job_message(job), reason=str(e))
            await self._update_job_state(job, JobState.FAILED, error_message=str(e))
            await self._complete_attempt(attempt_id, JobState.FAILED, error_message=str(e))

        except ScrapeFailure as e:
            err = e.message if hasattr(e, 'message') else str(e)
            logger.error(f"Spacescraper Job {job.job_id} failed: {err}")
            await metrics_tracker.record_job_status(success=False)
            await self.stream_queue.push_dlq("jobs_stream", self._job_message(job), reason=err)
            await self._update_job_state(job, JobState.FAILED, error_message=err)
            await self._complete_attempt(attempt_id, JobState.FAILED, error_message=err)

        except Exception as e:
            logger.exception(f"Spacescraper Critical Flow Fault on {job.job_id}: {e}")
            await metrics_tracker.record_job_status(success=False)
            await self.stream_queue.push_dlq("jobs_stream", self._job_message(job), reason=str(e))
            await self._update_job_state(job, JobState.FAILED, error_message=str(e))
            await self._complete_attempt(attempt_id, JobState.FAILED, error_message=str(e))

        finally:
            # Consistently release engine resources
            await engine.close()

    async def _perform_turbo_scrape(
        self, job: ScrapeJob, endpoints: list[dict]
    ) -> RawScrapePayload | None:
        """
        Low-Latency API Fallback.
        Replays previously discovered API endpoints for this domain over
        plain HTTP — never job.url itself, which returns the page's HTML.
        Returns None (never raises for a plain miss) when nothing replays;
        the caller falls through to a full browser fetch in the same job.
        """
        json_payloads = []
        last_status = 0
        for endpoint in endpoints:
            url = endpoint["url"]
            try:
                response = await target_http.get(url)
                last_status = response.status_code
                if not (200 <= response.status_code < 300):
                    continue
                content_type = response.headers.get("content-type", "").lower()
                if not content_type.split(";", 1)[0].strip().endswith("json"):
                    continue
                json_payloads.append({"url": url, "data": response.json()})
            except Exception as e:
                logger.debug(f"Spacescraper Turbo: endpoint replay failed for {url}: {e}", exc_info=True)

        if not json_payloads:
            return None

        await metrics_tracker.increment("turbo_mode_hits")
        return RawScrapePayload(
            job_id=job.job_id,
            target_site=job.target_site,
            url=job.url,
            status_code=last_status or 200,
            html_content="",
            json_payloads=json_payloads,
            correlation_id=job.correlation_id,
        )

    async def process_stream_message(self, message: QueueMessage) -> bool:
        """Callback for Valkey Stream consumer. Deserializes and dispatches to process_job."""
        try:
            fields = dict(message.payload)
            fields.setdefault("job_id", message.root_job_id or "")
            job = ScrapeJob(**fields)
            await self.process_job(job)
            return True
        except Exception as e:
            logger.error("Stream message processing failed: %s", e)
            return False

    async def run(self):
        """
        Main bootstrap method for the scraper node.
        Warms up the pool and starts the long-polling loop against Valkey.
        """
        logger.info("🚀 Spacescraper Scraper Node initializing...")

        await metrics_tracker.initialize()
        await self.job_repo.initialize()
        await self.obs_repo.initialize()
        await self.context_pool.initialize()

        logger.info("Spacescraper linked to Valkey. Connecting queues...")
        await self.stream_queue.connect()

        try:
            await self.stream_queue.consume(
                "jobs_stream", "scrapers", "scraper-1",
                self.process_stream_message,
            )
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            logger.info("Spacescraper Node: Graceful shutdown sequence triggered.")
        finally:
            pool_metrics = await self.context_pool.get_metrics()
            tracker_metrics = await metrics_tracker.get_metrics()
            logger.info(f"Spacescraper Session Stats: {tracker_metrics}")
            logger.info(f"Spacescraper Pool Stats: {pool_metrics}")
            await self.context_pool.close_all()
            await metrics_tracker.close()
            await self.job_repo.close()
            await self.obs_repo.close()
            if self._owns_stream_queue:
                await self.stream_queue.close()
            await target_http.close()
            await internal_http.close()


if __name__ == "__main__":
    worker = ScraperWorkerService()
    try:
        asyncio.run(worker.run())
    except KeyboardInterrupt:
        pass
