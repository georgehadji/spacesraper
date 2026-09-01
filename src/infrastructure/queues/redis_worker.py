# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Asynchronous Queuing System)
# Role: Interfaces with Redis to manage distributed task distribution (BLPOP/RPUSH).

import asyncio
import json
import logging
from typing import Optional, Callable, Any
from src.domain.models import ScrapeJob, RawScrapePayload, DiscoveryEvent
import valkey.asyncio as valkey

# Module-level logger for queue transactions
logger = logging.getLogger("Spacescraper.QueueWorker")

# Fail-closed constant: when Redis is unavailable, allow only this many jobs per root
FANOUT_DEGRADED_LIMIT = 10

_METRICS_PREFIX = "metrics:"  # Must match ObservabilityMetrics.prefix in observability.py

class RedisQueueWorker:
    """
    Spacescraper Orchestration Adapter.
    This component handles the production and consumption of Pydantic-validated 
    messages through Redis. It supports automated fallback to FakeRedis 
    (In-memory) for development environments without a live Redis server.
    """
    
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis_url = redis_url
        self._is_mock = False
        self.memory_limit_mb = 512 # Soft limit for backpressure
        try:
            # Initialize async redis client (decode_responses=True for JSON string handling)
            self.redis = valkey.from_url(redis_url, decode_responses=True)
            # Connectivity is verified later in connect() to allow lazy loading
        except Exception:
            self._setup_mock()

    def _setup_mock(self):
        """Initializes an in-memory Redis mock for isolated local development."""
        try:
            import fakeredis.aioredis
            self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
            self._is_mock = True
            logger.info("Spacescraper: Initialized In-Memory Queue (Offline Mode).")
        except ImportError:
            logger.error("Spacescraper: 'fakeredis' missing. Queue operations will be disabled.")
            self.redis = None

    async def connect(self):
        """Performs a heartbeat check to verify the Redis cluster is alive."""
        if self._is_mock: return
        try:
            await self.redis.ping()
            logger.info(f"Spacescraper: Queue heartbeat success at {self.redis_url}")
        except Exception as e:
            logger.warning(f"Spacescraper: Live queue unreachable ({e}). Switching to Offline Mode.")
            self._setup_mock()

    async def push_job(self, queue_name: str, job: ScrapeJob):
        """
        Serializes and pushes a ScrapeJob into the specified Redis list.
        Scenario 3: Memory-Aware Backpressure Guardrail.
        """
        if not self.redis: return
        
        # 1. Backpressure Check: Is the cluster saturated?
        if not self._is_mock:
            try:
                info = await self.redis.info(section="memory")
                used_memory = info.get("used_memory_rss", 0) / (1024 * 1024) # MB
                
                if used_memory > self.memory_limit_mb:
                    logger.warning(f"Spacescraper Backpressure ALERT: Redis memory usage ({used_memory:.1f}MB) exceeds threshold. Throttling ingestion.")
                    # Drop non-critical jobs if we are near saturation
                    if used_memory > (self.memory_limit_mb * 1.5):  # Hard limit
                        logger.error(
                            f"Spacescraper CRITICAL: Cluster Saturation. Routing job {job.job_id} to DLQ to prevent OOM crash."
                        )
                        await self.push_dead_letter(queue_name, job, reason="OOM_BACKPRESSURE")
                        # NOTE: Written directly to Redis (not via metrics_tracker) to avoid a circular import.
                        # metrics_tracker.metrics (sync property) will report 0 for this key;
                        # use metrics_tracker.get_metrics() (async) to see the accurate count.
                        await self.redis.incrby(_METRICS_PREFIX + "jobs_dropped_oom", 1)
                        return
            except Exception as e:
                logger.debug(f"Backpressure monitor failed: {e}")

        payload = job.model_dump_json()
        await self.redis.rpush(queue_name, payload)
        logger.debug(f"Spacescraper: Enqueued JOB {job.job_id} -> {queue_name}")

    async def poll_jobs(self, queue_name: str, callback: Callable[[ScrapeJob], Any]):
        """
        Consumes jobs from a queue using blocking long-polling (BLPOP).
        Automatically deserializes payloads into ScrapeJob models.
        """
        if not self.redis: return
        logger.info(f"Spacescraper: Node listening on queue [{queue_name}]...")
        while True:
            try:
                # 1 second timeout to allow for clean signal handling
                result = await self.redis.blpop(queue_name, timeout=1)
                if result:
                    _, payload_str = result
                    job = ScrapeJob(**json.loads(payload_str))
                    # Pass the job object to the worker callback
                    await callback(job)
            except Exception as e:
                logger.error(f"Spacescraper Consumption Error: {e}")
                import asyncio
                await asyncio.sleep(2) # Backoff on repeated errors

    async def push_raw_payload(self, queue_name: str, payload: RawScrapePayload):
        """Pushes raw scraped data to the processor for downstream ETL."""
        if not self.redis: return
        await self.redis.rpush(queue_name, payload.model_dump_json())

    async def poll_raw_payloads(self, queue_name: str, callback: Callable[[RawScrapePayload], Any]):
        """Consumes raw payload shipments for the processor node."""
        if not self.redis: return
        logger.info(f"Spacescraper: Node listening on raw data stream [{queue_name}]...")
        while True:
            try:
                result = await self.redis.blpop(queue_name, timeout=1)
                if result:
                    _, payload_str = result
                    payload = RawScrapePayload(**json.loads(payload_str))
                    await callback(payload)
            except Exception as e:
                logger.error(f"Spacescraper Raw Ingest Error: {e}")
                await asyncio.sleep(2)

    async def push_event(self, queue_name: str, event: DiscoveryEvent):
        """Publishes a discovery event for downstream side-effects (Reporting, Slack)."""
        if not self.redis: return
        await self.redis.rpush(queue_name, event.model_dump_json())
        logger.debug(f"Spacescraper: Published EVENT {event.event_id}")

    async def poll_events(self, queue_name: str, callback: Callable[[DiscoveryEvent], Any]):
        """Listens for discovery events and triggers plugins."""
        if not self.redis: return
        logger.info(f"Spacescraper: Node listening on event stream [{queue_name}]...")
        while True:
            try:
                result = await self.redis.blpop(queue_name, timeout=1)
                if result:
                    _, payload_str = result
                    event = DiscoveryEvent(**json.loads(payload_str))
                    await callback(event)
            except Exception as e:
                logger.error(f"Spacescraper Event Poll Error: {e}")
                await asyncio.sleep(2)

    async def push_dead_letter(self, original_queue: str, item: Any, reason: str):
        """
        Quarantine Logic.
        Failed jobs are pushed to a DLQ for manual inspection or retry logic.
        """
        dlq_name = f"{original_queue}_dlq"
        if not self.redis: return
        try:
            # Handle both models and strings
            payload = item.model_dump_json() if hasattr(item, 'model_dump_json') else str(item)
            entry = json.dumps({
                "error": reason,
                "data": payload
            })
            await self.redis.rpush(dlq_name, entry)
            logger.warning(f"Spacescraper Quarantine: Moved failed item to [{dlq_name}]: {reason}")
        except Exception as e:
            logger.error(f"Spacescraper DLQ fault: {e}")

    async def get_allowed_fanout(self, root_job_id: str, requested: int, max_fanout: int) -> int:
        """
        Atomic fan-out budget check via Lua script.
        Returns how many of `requested` child jobs are allowed under the per-root cap.
        Uses Redis EVAL for atomic read-modify-write; fails closed on error.

        In mock/dev mode: enforces the cap in-process rather than returning requested.
        On Redis error: returns FANOUT_DEGRADED_LIMIT (conservative constant).
        """
        fanout_key = f"fanout:{root_job_id}"

        # Mock/dev mode: enforce the cap locally
        if self._is_mock:
            # Simple in-process tracking (per instance, not shared across replicas)
            if not hasattr(self, '_fanout_tracker'):
                self._fanout_tracker = {}

            current = self._fanout_tracker.get(root_job_id, 0)
            available = max(0, max_fanout - current)
            allowed = min(requested, available)

            if allowed > 0:
                self._fanout_tracker[root_job_id] = current + allowed

            return allowed

        if not self.redis:
            # Redis unavailable and not mock mode: fail closed
            logger.warning(f"Fan-out cap: Redis unavailable for {root_job_id}, allowing degraded limit of {FANOUT_DEGRADED_LIMIT}")
            await self.redis.incrby(_METRICS_PREFIX + "fanout_degraded_total", 1)
            return min(requested, FANOUT_DEGRADED_LIMIT)

        lua_script = "\n".join([
            "local current = tonumber(redis.call('GET', KEYS[1]) or '0')",
            "local available = math.max(0, tonumber(ARGV[2]) - current)",
            "local allowed = math.min(tonumber(ARGV[1]), available)",
            "if allowed > 0 then",
            "    redis.call('INCRBY', KEYS[1], allowed)",
            "    redis.call('EXPIRE', KEYS[1], 3600)",
            "end",
            "return allowed",
        ])
        try:
            redis_eval = getattr(self.redis, "eval")
            result = await redis_eval(lua_script, 1, fanout_key, str(requested), str(max_fanout))
            return int(result)
        except Exception as e:
            # Fail closed: return degraded limit and log the incident
            logger.warning(
                f"Fan-out check failed ({e}) for {root_job_id}, restricting to degraded limit {FANOUT_DEGRADED_LIMIT}"
            )
            try:
                await self.redis.incrby(_METRICS_PREFIX + "fanout_degraded_total", 1)
            except Exception:
                pass  # Best-effort metric increment
            return min(requested, FANOUT_DEGRADED_LIMIT)

    async def close(self):
        """Cleanly closes the async Redis connection."""
        if self.redis:
            await self.redis.aclose()
        logger.info("Spacescraper Queue: Closed Redis link.")
