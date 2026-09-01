# Author: Spacescraper (Discovery Worker)
# Role: Consumes DISCOVERY_QUERY messages from research_stream, runs the
#       DiscoveryService filter pipeline, archives the raw SERP, and emits
#       ordinary ScrapeJobs onto the existing jobs_queue. The scraper,
#       processor, and reporter never learn a job originated from search.

import asyncio
import logging

from src.infrastructure.queues.redis_worker import RedisQueueWorker
from src.infrastructure.queues.stream_queue import RedisStreamQueue
from src.infrastructure.repositories.research_plan_repository import SqliteResearchPlanRepository
from src.infrastructure.artifact_store import LocalArtifactStore
from src.infrastructure.monitoring.observability import metrics_tracker
from src.infrastructure.http_client import http_client
from src.infrastructure.logger_config import setup_production_logging

from src.application.discovery_service import DiscoveryService
from src.security.url_policy import UrlPolicy
from src.domain.models import QueueMessage, ResearchPlan, JobState
from src.domain.exceptions import DiscoveryRefusedError
from src.config_settings import settings

from src.infrastructure.providers.search_provider import (
    NoOpSearchProvider,
    DuckDuckGoSearchProvider,
    SerperSearchProvider,
)

setup_production_logging()
logger = logging.getLogger("Spacescraper.DiscoveryWorker")


def _build_search_provider():
    """Composition root: concrete SearchProvider chosen here from settings, once."""
    provider_name = settings.discovery.search_provider
    if provider_name == "duckduckgo":
        return DuckDuckGoSearchProvider()
    if provider_name == "serper":
        return SerperSearchProvider(api_key=settings.discovery.search_api_key)
    return NoOpSearchProvider()


class DiscoveryWorkerService:
    """Spacescraper Node: Discovery. Turns a search query into scoped ScrapeJobs."""

    def __init__(self):
        self.queue = RedisQueueWorker()
        self.stream_queue = RedisStreamQueue()
        self.plan_repo = SqliteResearchPlanRepository()
        self.artifact_store = LocalArtifactStore()

        # Composition root: concretes chosen here, injected as ports.
        self.search_provider = _build_search_provider()
        self.url_policy = UrlPolicy(
            allowlist=list(settings.discovery.allowed_domains),
            denylist=list(settings.discovery.denied_domains),
            respect_robots=settings.discovery.respect_robots,
        )
        self.discovery_service = DiscoveryService(
            search_provider=self.search_provider,
            url_policy=self.url_policy,
            queue=self.queue,
            discovery_max_fanout=settings.discovery.max_fanout,
        )

    async def handle_discovery_query(self, message: QueueMessage) -> bool:
        """Callback for the research_stream consumer. Returns True on success."""
        payload = message.payload
        plan_id = payload.get("plan_id", "")

        if not settings.features.get("discovery", False):
            logger.warning("Discovery disabled; skipping plan %s", plan_id)
            return True  # ack — not a transient failure, don't retry

        plan = await self.plan_repo.get_plan(plan_id)
        if plan is None:
            logger.error("Discovery plan %s not found; dropping message", plan_id)
            return True  # unrecoverable — acking avoids an infinite retry loop

        try:
            await self.plan_repo.update_plan_state(plan_id, JobState.RUNNING)

            jobs, rejections = await self.discovery_service.discover(plan)

            # Archive raw SERP for replay, referenced by serp_artifact_sha.
            hits = await self.search_provider.search(plan.query, max_results=plan.max_results)
            import json
            raw_serp = json.dumps([h.model_dump() for h in hits]).encode("utf-8")
            sha256 = await self.artifact_store.store(
                raw_serp, original_url=f"discovery:{plan_id}",
                content_type="application/json", job_id=plan_id,
            )
            await self.plan_repo.set_serp_artifact_sha(plan_id, sha256)

            child_job_ids = []
            for job in jobs:
                await self.queue.push_job("jobs_queue", job)
                child_job_ids.append(job.job_id)

            await self.plan_repo.set_child_job_ids(plan_id, child_job_ids)
            await self.plan_repo.update_plan_state(plan_id, JobState.SUCCEEDED)

            await metrics_tracker.increment("discovery_hits_total", len(hits))
            for reason, count in rejections.items():
                if count:
                    await metrics_tracker.increment(f"discovery_rejected_total_{reason}", count)

            logger.info(
                "Discovery plan %s: %d jobs enqueued, %d hits, rejections=%s",
                plan_id, len(child_job_ids), len(hits), rejections,
            )
            return True

        except DiscoveryRefusedError as e:
            logger.warning("Discovery plan %s refused: %s", plan_id, e)
            await self.plan_repo.update_plan_state(plan_id, JobState.FAILED, error_message=str(e))
            return True  # refusal is a policy outcome, not a transient failure

        except Exception as e:
            logger.exception("Discovery plan %s failed: %s", plan_id, e)
            await self.plan_repo.update_plan_state(plan_id, JobState.FAILED, error_message=str(e))
            return False  # transient — let the stream consumer retry/DLQ

    async def run(self):
        """Main loop."""
        logger.info("Spacescraper Discovery Worker standby...")
        await self.plan_repo.initialize()
        await self.queue.connect()
        await self.stream_queue.connect()
        try:
            await self.stream_queue.consume(
                "research_stream", "discovery_workers", "discovery-1",
                self.handle_discovery_query,
            )
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            pass
        finally:
            await metrics_tracker.close()
            await self.plan_repo.close()
            await self.stream_queue.close()
            await self.queue.close()
            await http_client.close()


if __name__ == "__main__":
    worker = DiscoveryWorkerService()
    asyncio.run(worker.run())
