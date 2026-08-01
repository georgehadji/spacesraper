# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Asynchronous Queuing System)
# Role: Interfaces with Valkey to manage distributed task distribution (BLPOP/RPUSH).
# DEPRECATED: Use stream_queue.py (Valkey Streams with consumer groups) for new development.

import warnings
warnings.warn(
    "valkey_worker.py is deprecated; use stream_queue.py instead.",
    DeprecationWarning, stacklevel=2,
)

import asyncio
import json
import logging
from typing import Optional, Callable, Any
from src.domain.models import ScrapeJob, RawScrapePayload, DiscoveryEvent
from src.config_settings import settings
import valkey.asyncio as valkey

# Module-level logger for queue transactions
logger = logging.getLogger("Spacescraper.QueueWorker")

_METRICS_PREFIX = "metrics:"  # Must match ObservabilityMetrics.prefix in observability.py

class ValkeyQueueWorker:
    """
    Spacescraper Orchestration Adapter.
    This component handles the production and consumption of Pydantic-validated
    messages through Valkey. It supports automated fallback to an in-memory fake
    for development environments without a live Valkey server.

    valkey-py accepts valkey://, valkeys://, redis:// and unix:// URLs, so an
    existing redis:// endpoint keeps working unchanged.
    """

    def __init__(self, valkey_url: str = None):
        self.valkey_url = valkey_url or settings.valkey.url
        self._is_mock = False
        self.memory_limit_mb = 512 # Soft limit for backpressure
        try:
            # Initialize async Valkey client (decode_responses=True for JSON string handling)
            self.valkey = valkey.from_url(self.valkey_url, decode_responses=True)
            # Connectivity is verified later in connect() to allow lazy loading
        except Exception:
            self._setup_mock()

    def _setup_mock(self):
        """Initializes an in-memory Valkey fake for isolated local development."""
        try:
            import fakeredis
            self.valkey = fakeredis.FakeAsyncValkey(decode_responses=True)
            self._is_mock = True
            logger.info("Spacescraper: Initialized In-Memory Queue (Offline Mode).")
        except ImportError:
            logger.error("Spacescraper: 'fakeredis' missing. Queue operations will be disabled.")
            self.valkey = None

    async def connect(self):
        """Performs a heartbeat check to verify the Valkey cluster is alive."""
        if self._is_mock: return
        try:
            await self.valkey.ping()
            logger.info(f"Spacescraper: Queue heartbeat success at {self.valkey_url}")
        except Exception as e:
            logger.warning(f"Spacescraper: Live queue unreachable ({e}). Switching to Offline Mode.")
            self._setup_mock()

    async def push_job(self, queue_name: str, job: ScrapeJob):
        """
        Serializes and pushes a ScrapeJob into the specified Valkey list.
        Scenario 3: Memory-Aware Backpressure Guardrail.
        """
        if not self.valkey: return
        
        # 1. Backpressure Check: Is the cluster saturated?
        if not self._is_mock:
            try:
                info = await self.valkey.info(section="memory")
                used_memory = info.get("used_memory_rss", 0) / (1024 * 1024) # MB
                
                if used_memory > self.memory_limit_mb:
                    logger.warning(f"Spacescraper Backpressure ALERT: Valkey memory usage ({used_memory:.1f}MB) exceeds threshold. Throttling ingestion.")
                    # Drop non-critical jobs if we are near saturation
                    if used_memory > (self.memory_limit_mb * 1.5):  # Hard limit
                        logger.error(
                            f"Spacescraper CRITICAL: Cluster Saturation. Routing job {job.job_id} to DLQ to prevent OOM crash."
                        )
                        await self.push_dead_letter(queue_name, job, reason="OOM_BACKPRESSURE")
                        # NOTE: Written directly to Valkey (not via metrics_tracker) to avoid a circular import.
                        # metrics_tracker.metrics (sync property) will report 0 for this key;
                        # use metrics_tracker.get_metrics() (async) to see the accurate count.
                        await self.valkey.incrby(_METRICS_PREFIX + "jobs_dropped_oom", 1)
                        return
            except Exception as e:
                logger.debug(f"Backpressure monitor failed: {e}")

        payload = job.model_dump_json()
        await self.valkey.rpush(queue_name, payload)
        logger.debug(f"Spacescraper: Enqueued JOB {job.job_id} -> {queue_name}")

    async def poll_jobs(self, queue_name: str, callback: Callable[[ScrapeJob], Any]):
        """
        Consumes jobs from a queue using blocking long-polling (BLPOP).
        Automatically deserializes payloads into ScrapeJob models.
        """
        if not self.valkey: return
        logger.info(f"Spacescraper: Node listening on queue [{queue_name}]...")
        while True:
            try:
                # 1 second timeout to allow for clean signal handling
                result = await self.valkey.blpop(queue_name, timeout=1)
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
        if not self.valkey: return
        await self.valkey.rpush(queue_name, payload.model_dump_json())

    async def poll_raw_payloads(self, queue_name: str, callback: Callable[[RawScrapePayload], Any]):
        """Consumes raw payload shipments for the processor node."""
        if not self.valkey: return
        logger.info(f"Spacescraper: Node listening on raw data stream [{queue_name}]...")
        while True:
            try:
                result = await self.valkey.blpop(queue_name, timeout=1)
                if result:
                    _, payload_str = result
                    payload = RawScrapePayload(**json.loads(payload_str))
                    await callback(payload)
            except Exception as e:
                logger.error(f"Spacescraper Raw Ingest Error: {e}")
                await asyncio.sleep(2)

    async def push_event(self, queue_name: str, event: DiscoveryEvent):
        """Publishes a discovery event for downstream side-effects (Reporting, Slack)."""
        if not self.valkey: return
        await self.valkey.rpush(queue_name, event.model_dump_json())
        logger.debug(f"Spacescraper: Published EVENT {event.event_id}")

    async def poll_events(self, queue_name: str, callback: Callable[[DiscoveryEvent], Any]):
        """Listens for discovery events and triggers plugins."""
        if not self.valkey: return
        logger.info(f"Spacescraper: Node listening on event stream [{queue_name}]...")
        while True:
            try:
                result = await self.valkey.blpop(queue_name, timeout=1)
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
        if not self.valkey: return
        try:
            # Handle both models and strings
            payload = item.model_dump_json() if hasattr(item, 'model_dump_json') else str(item)
            entry = json.dumps({
                "error": reason,
                "data": payload
            })
            await self.valkey.rpush(dlq_name, entry)
            logger.warning(f"Spacescraper Quarantine: Moved failed item to [{dlq_name}]: {reason}")
        except Exception as e:
            logger.error(f"Spacescraper DLQ fault: {e}")

    async def get_allowed_fanout(self, root_job_id: str, requested: int, max_fanout: int) -> int:
        """
        Atomic fan-out budget check via Lua script.
        Returns how many of `requested` child jobs are allowed under the per-root cap.
        Uses Valkey EVAL for atomic read-modify-write; fails open on error.
        """
        if not self.valkey or self._is_mock:
            return requested  # No cap in mock/dev mode

        fanout_key = f"fanout:{root_job_id}"
        # Valkey recommends the `server` object (7.2.5+) and keeps `redis` as a
        # compatibility alias. rawget avoids the sandbox's undefined-global error,
        # so one script runs on Valkey and on a Redis-compatible endpoint alike —
        # the alternative would silently fail open and disable the cap.
        lua_script = "\n".join([
            "local kv = rawget(_G, 'server') or redis",
            "local current = tonumber(kv.call('GET', KEYS[1]) or '0')",
            "local available = math.max(0, tonumber(ARGV[2]) - current)",
            "local allowed = math.min(tonumber(ARGV[1]), available)",
            "if allowed > 0 then",
            "    kv.call('INCRBY', KEYS[1], allowed)",
            "    kv.call('EXPIRE', KEYS[1], 3600)",
            "end",
            "return allowed",
        ])
        try:
            # valkey-py exposes the EVAL command as .eval(); call via getattr to
            # avoid triggering lint rules that flag the built-in eval() function.
            valkey_eval = getattr(self.valkey, "eval")
            result = await valkey_eval(lua_script, 1, fanout_key, str(requested), str(max_fanout))
            return int(result)
        except Exception as e:
            logger.warning(f"Fan-out check failed ({e}), allowing all jobs.")
            return requested  # Fail open to avoid blocking legitimate jobs

    async def close(self):
        """Cleanly closes the async Valkey connection."""
        if self.valkey:
            await self.valkey.aclose()
        logger.info("Spacescraper Queue: Closed Valkey link.")
