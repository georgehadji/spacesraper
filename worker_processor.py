# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Data Processing Worker)
# Role: Consumes raw web payloads, resolves entities via Pure Extraction Kernel, 
#       and delegates side-effects to the Post-Processor Hub.

import asyncio
import logging
import os
import socket

from src.application.extraction_pipeline import DeterministicExtractionPipeline, ExtractionPipeline

# Side-Effect Hub
from src.application.post_processor import IntelligencePostProcessor
from src.domain.exceptions import ExtractionError
from src.domain.models import DiscoveryEvent, ExtractedRecord, MessageType, QueueMessage, RawScrapePayload, ScrapeJob
from src.infrastructure.http_client import internal_http, target_http
from src.infrastructure.logger_config import setup_production_logging
from src.infrastructure.middleware.correlation import set_request_id
from src.infrastructure.monitoring.observability import metrics_tracker
from src.infrastructure.queues.stream_queue import ValkeyStreamQueue, make_message
from src.infrastructure.repositories.job_repository import SqliteJobRepository
from src.infrastructure.repositories.overlay_repository import SqliteOverlayRepository
from src.infrastructure.repositories.record_repository import SqliteRecordRepository
from src.infrastructure.storage.sqlite_tracker import SqliteTracker

# setup_production_logging()
logger = logging.getLogger("Spacescraper.Processor")

class ProcessorWorkerService:
    """
    Spacescraper Node: Data Processor (Refactored).
    Implements a clean pipeline-to-post-processor handover.
    """
    MAX_RECURSIVE_FANOUT = 200  # max child jobs per root job to prevent OOM floods

    def __init__(
        self,
        stream_queue: ValkeyStreamQueue = None,
        job_repo: SqliteJobRepository = None,
        record_repo: SqliteRecordRepository = None,
        overlay_repo: SqliteOverlayRepository = None,
        intel_tracker: SqliteTracker = None,
    ):
        # Every dependency below is optional and self-constructs when omitted
        # (production default); tests inject fakes/shared instances instead of
        # constructing the service then overwriting its attributes (W4.4).
        # An injected queue is owned by the caller; a self-created one is closed here.
        # Offline, each fallback client owns a private in-memory store, so the
        # scraper and processor must share one instance to exchange payloads.
        self._owns_stream_queue = stream_queue is None
        self.stream_queue = stream_queue or ValkeyStreamQueue()
        self.pipeline = ExtractionPipeline()
        self.intel_tracker = intel_tracker or SqliteTracker()
        self.post_processor = IntelligencePostProcessor(intel_tracker=self.intel_tracker)
        self.job_repo = job_repo or SqliteJobRepository()
        self.record_repo = record_repo or SqliteRecordRepository()
        self.overlay_repo = overlay_repo or SqliteOverlayRepository()
        # P2: per-root-job seen-URL set, so a crawl doesn't re-enqueue a page
        # it already discovered via two different link paths. In-memory,
        # single-process — a multi-replica processor deployment would need
        # this in Valkey instead (a SET per root_job_id); out of scope here.
        self._seen_urls: dict[str, set[str]] = {}

        # Optimized Strategy Registry (Strictly Declarative)
        # Live OverlayRepository lookup (W3.1/C1) — an overlay promoted to ACTIVE
        # via POST /overlays/{id}/promote now has real effect on extraction.
        deterministic_strategy = DeterministicExtractionPipeline(overlay_repo=self.overlay_repo)
        self.strategies = {
            "universal": deterministic_strategy
        }

    async def process_payload(self, payload: RawScrapePayload) -> None:
        """Processes a raw data shipment and emits intelligence signals."""
        # Propagate correlation ID for end-to-end tracing
        if payload.correlation_id:
            set_request_id(payload.correlation_id)
        await metrics_tracker.increment("jobs_total")
        
        # 1. Extraction Phase (Pure Logic)
        strategy = self.strategies.get(payload.target_site, self.strategies["universal"])
        result = await self.pipeline.process(payload, strategy)
        
        if not result.success:
            await metrics_tracker.record_job_status(success=False)
            logger.error(f"Spacescraper Intelligence Fault: {result.error}")
            return

        await metrics_tracker.record_job_status(success=True)
        
        # 2. Audit Phase (State Persistence)
        status_counts, audited_records = await self.post_processor.run_state_audit(result.entities)
        
        # 2b. Persist generic ExtractedRecord entities
        record_count = 0
        for entity in result.entities:
            if isinstance(entity, ExtractedRecord):
                await self.record_repo.create_record(entity, job_id=payload.job_id)
                record_count += 1

        # 3. Intelligence Signaling (Event Hub)
        if status_counts["NEW"] > 0 or status_counts["UPDATED"] > 0:
            discovery_event = DiscoveryEvent(
                job_id=payload.job_id,
                target_site=payload.target_site,
                new_count=status_counts["NEW"],
                updated_count=status_counts["UPDATED"],
                entities=audited_records
            )
            await self.stream_queue.push(
                "discovery_stream",
                make_message(
                    MessageType.DISCOVERY_EVENT,
                    discovery_event.model_dump(mode="json"),
                    correlation_id=payload.correlation_id,
                    root_job_id=payload.job_id,
                ),
            )
            logger.info(f"Spacescraper: Emitted DISCOVERY_SIGNAL for {payload.job_id}")

        # 4. Discovery Phase (Recursive Handover — Fan-Out Capped)
        if result.follow_urls:
            root_id = payload.job_id
            while root_id.startswith("rec_"):
                root_id = root_id[4:]  # strip "rec_" prefix
            if not root_id:
                root_id = payload.job_id  # fallback to full job_id if stripping produced empty string

            # P2: dedup against this crawl tree's already-seen URLs before
            # spending fan-out budget on a revisit.
            seen = self._seen_urls.setdefault(root_id, set())
            fresh_follows = [f for f in result.follow_urls if f["url"] not in seen]
            seen.update(f["url"] for f in fresh_follows)

            allowed_count = await self.stream_queue.get_allowed_fanout(
                root_id, len(fresh_follows), self.MAX_RECURSIVE_FANOUT
            )
            dropped_count = len(fresh_follows) - allowed_count

            def _child_job(follow: dict, job_id: str) -> ScrapeJob:
                return ScrapeJob(
                    job_id=job_id,
                    url=follow['url'],
                    target_site=follow['target_site'],
                    depth=follow.get('depth', 0),
                    max_depth=follow.get('max_depth', payload.max_depth),
                    persona_id=getattr(payload, 'persona_id', None),
                    overlay=getattr(payload, 'overlay', None),
                    webhook_url=getattr(payload, 'webhook_url', None),
                    correlation_id=payload.correlation_id,
                    follow_links=payload.follow_links,
                    link_include_globs=payload.link_include_globs,
                    link_exclude_globs=payload.link_exclude_globs,
                )

            for follow in fresh_follows[:allowed_count]:
                new_job = _child_job(follow, f"rec_{payload.job_id}")
                await self.stream_queue.push(
                    "jobs_stream",
                    make_message(
                        MessageType.SCRAPE_JOB,
                        new_job.model_dump(mode="json"),
                        correlation_id=payload.correlation_id,
                        root_job_id=root_id,
                    ),
                )

            if dropped_count > 0:
                logger.warning(
                    f"Spacescraper: Fan-out cap hit for root {root_id}. "
                    f"Allowed {allowed_count}/{len(fresh_follows)} recursive jobs. "
                    f"Dropping {dropped_count} to DLQ."
                )
                for follow in fresh_follows[allowed_count:]:
                    overflow_job = _child_job(follow, f"rec_{payload.job_id}")
                    await self.stream_queue.push_dlq(
                        "jobs_stream",
                        make_message(
                            MessageType.SCRAPE_JOB,
                            overflow_job.model_dump(mode="json"),
                            correlation_id=payload.correlation_id,
                            root_job_id=root_id,
                        ),
                        reason="FANOUT_CAP_EXCEEDED",
                    )
                await metrics_tracker.increment("fanout_cap_drops", dropped_count)

        logger.info(f"Spacescraper: Run {payload.job_id} complete. Audit: {status_counts}")

        # Update job record count in durable state
        total = status_counts.get("NEW", 0) + status_counts.get("UPDATED", 0) + status_counts.get("UNCHANGED", 0)
        if total > 0:
            await self.job_repo.update_job_record_count(payload.job_id, total)

    async def process_stream_message(self, message: QueueMessage) -> bool:
        """Callback for Valkey Stream consumer."""
        try:
            fields = dict(message.payload)
            fields.setdefault("job_id", message.root_job_id or "")
            raw = RawScrapePayload(**fields)
            await self.process_payload(raw)
            return True
        except Exception as e:
            logger.error("Stream message processing failed: %s", e)
            return False

    async def run(self):
        """Main loop."""
        logger.info("🚀 Spacescraper Intelligence Processor (Option 1) standby...")
        await self.job_repo.initialize()
        await self.record_repo.initialize()
        await self.intel_tracker.initialize()
        await self.overlay_repo.initialize()
        await self.stream_queue.connect()
        consumer_name = f"processor-{socket.gethostname()}-{os.getpid()}"
        try:
            await self.stream_queue.consume(
                "raw_data_stream", "processors", consumer_name,
                self.process_stream_message,
            )
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            pass
        finally:
            await metrics_tracker.close()
            await self.job_repo.close()
            await self.record_repo.close()
            await self.intel_tracker.close()
            await self.overlay_repo.close()
            if self._owns_stream_queue:
                await self.stream_queue.close()
            await target_http.close()
            await internal_http.close()

if __name__ == "__main__":
    from src.bootstrap import container as _container

    worker = ProcessorWorkerService(
        stream_queue=_container.stream_queue,
        job_repo=_container.job_repo,
        record_repo=_container.record_repo,
        overlay_repo=_container.overlay_repo,
    )
    asyncio.run(worker.run())
