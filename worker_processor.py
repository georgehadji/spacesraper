# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Data Processing Worker)
# Role: Consumes raw web payloads, resolves entities via Pure Extraction Kernel, 
#       and delegates side-effects to the Post-Processor Hub.

import asyncio
import logging
from typing import Dict, Any

from src.infrastructure.queues.redis_worker import RedisQueueWorker
from src.application.pipeline import DataPipeline
from src.domain.models import RawScrapePayload, ScrapeJob, DiscoveryEvent
from src.infrastructure.monitoring.observability import metrics_tracker
from src.infrastructure.storage.sqlite_tracker import intel_tracker
from src.infrastructure.http_client import http_client

# Strategy Kernel
from src.extractors.universal_strategy import UniversalExtractionStrategy

# Side-Effect Hub
from src.application.post_processor import IntelligencePostProcessor

from src.infrastructure.logger_config import setup_production_logging
from src.domain.exceptions import ExtractionError

# setup_production_logging()
logger = logging.getLogger("Spacescraper.Processor")

class ProcessorWorkerService:
    """
    Spacescraper Node: Data Processor (Refactored).
    Implements a clean pipeline-to-post-processor handover.
    """
    MAX_RECURSIVE_FANOUT = 200  # max child jobs per root job to prevent OOM floods

    def __init__(self):
        self.queue = RedisQueueWorker()
        self.pipeline = DataPipeline(ai_enrichment_enabled=True)
        self.post_processor = IntelligencePostProcessor()
        
        # Optimized Strategy Registry (Strictly Declarative)
        universal_strategy = UniversalExtractionStrategy()
        self.strategies = {
            "universal": universal_strategy
        }

    async def process_payload(self, payload: RawScrapePayload) -> None:
        """Processes a raw data shipment and emits intelligence signals."""
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
        status_counts, audited_tenders = await self.post_processor.run_state_audit(result.entities)
        
        # 3. Intelligence Signaling (Event Hub)
        if status_counts["NEW"] > 0 or status_counts["UPDATED"] > 0:
            discovery_event = DiscoveryEvent(
                job_id=payload.job_id,
                target_site=payload.target_site,
                new_count=status_counts["NEW"],
                updated_count=status_counts["UPDATED"],
                entities=audited_tenders
            )
            await self.queue.push_event("discovery_events_queue", discovery_event)
            logger.info(f"Spacescraper: Emitted DISCOVERY_SIGNAL for {payload.job_id}")

        # 4. Discovery Phase (Recursive Handover — Fan-Out Capped)
        if result.follow_urls:
            root_id = payload.job_id
            while root_id.startswith("rec_"):
                root_id = root_id[4:]  # strip "rec_" prefix
            if not root_id:
                root_id = payload.job_id  # fallback to full job_id if stripping produced empty string
            allowed_count = await self.queue.get_allowed_fanout(
                root_id, len(result.follow_urls), self.MAX_RECURSIVE_FANOUT
            )
            dropped_count = len(result.follow_urls) - allowed_count

            for follow in result.follow_urls[:allowed_count]:
                new_job = ScrapeJob(
                    job_id=f"rec_{payload.job_id}",
                    url=follow['url'],
                    target_site=follow['target_site'],
                    depth=follow.get('depth', 0),
                    persona_id=getattr(payload, 'persona_id', None),
                    overlay=getattr(payload, 'overlay', None),
                    webhook_url=getattr(payload, 'webhook_url', None),
                )
                await self.queue.push_job("jobs_queue", new_job)

            if dropped_count > 0:
                logger.warning(
                    f"Spacescraper: Fan-out cap hit for root {root_id}. "
                    f"Allowed {allowed_count}/{len(result.follow_urls)} recursive jobs. "
                    f"Dropping {dropped_count} to DLQ."
                )
                for follow in result.follow_urls[allowed_count:]:
                    overflow_job = ScrapeJob(
                        job_id=f"rec_{payload.job_id}",
                        url=follow['url'],
                        target_site=follow['target_site'],
                        depth=follow.get('depth', 0),
                    )
                    await self.queue.push_dead_letter("jobs_queue", overflow_job, reason="FANOUT_CAP_EXCEEDED")
                await metrics_tracker.increment("fanout_cap_drops", dropped_count)

        logger.info(f"Spacescraper: Run {payload.job_id} complete. Audit: {status_counts}")

    async def run(self):
        """Main loop."""
        logger.info("🚀 Spacescraper Intelligence Processor (Option 1) standby...")
        await intel_tracker.initialize()
        # Try to connect to Redis (falls back to fakeredis if unavailable)
        await self.queue.connect()
        try:
            await self.queue.poll_raw_payloads("raw_data_queue", self.process_payload)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            pass
        finally:
            await metrics_tracker.close()
            await self.queue.close()
            await http_client.close()

if __name__ == "__main__":
    worker = ProcessorWorkerService()
    asyncio.run(worker.run())
