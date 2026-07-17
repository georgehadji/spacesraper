# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Observability & Monitoring)
# Role: Collects cluster-wide metrics and triggers real-time alerts.

import asyncio
import logging
import os
from typing import Dict, Any, Optional
import valkey.asyncio as valkey

# Specialized logger for monitoring activities
logger = logging.getLogger("Spacescraper.Observability")

class ObservabilityMetrics:
    """
    Spacescraper Health & Telemetry Node.
    Responsible for tracking job counts, success rates, and external 
    failures (Proxies, Captchas). It utilizes Redis as a central 
    shared-state for metrics, providing a 'source of truth' for the dashboard.
    Uses async Redis operations to prevent blocking the event loop.
    """
    
    def __init__(self, redis_url: Optional[str] = None):
        # Configuration for the shared metrics store
        url = redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379")
        self.redis_url = url
        self._redis: Optional[valkey.Redis] = None
        self._is_mock = False
        self._lock = asyncio.Lock()
        
        # Local cache for metrics to reduce Redis calls
        self._local_cache: Dict[str, int] = {}
        self._cache_dirty = False
        
        # Defining keyspace for metrics storage to avoid collisions
        self.prefix = "metrics:"
        self.metric_keys = [
            "jobs_total", "jobs_success", "jobs_failed",
            "captcha_encountered", "proxy_failures",
            "pages_scraped", "llm_fallbacks_triggered",
            "turbo_yield_failure",   # domains demoted due to empty turbo responses
            "jobs_dropped_oom",      # jobs silently dropped under OOM (added in Task 3)
            "fanout_cap_drops",      # recursive jobs dropped at fan-out cap (added in Task 4)
        ]

    async def initialize(self):
        """Initialize Redis connection asynchronously."""
        try:
            self._redis = valkey.from_url(self.redis_url, decode_responses=True)
            await self._redis.ping()
            logger.info(f"Spacescraper: Telemetry linked to live storage at {self.redis_url}")
        except Exception as e:
            logger.warning(f"Spacescraper: Live storage unreachable ({e}). Initializing local fallback...")
            await self._setup_mock()

    async def _setup_mock(self):
        """Initializes an in-memory Redis mock for isolated local development."""
        try:
            import fakeredis.aioredis
            self._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
            self._is_mock = True
            logger.info("Spacescraper: Local metrics mode (In-Memory) active.")
        except ImportError:
            logger.error("Spacescraper: 'fakeredis' missing. Metrics will be lost on exit.")
            self._redis = None

    async def increment(self, metric_name: str, count: int = 1):
        """Atomically increments a specific counter in the metrics store."""
        if not self._redis:
            return
            
        async with self._lock:
            try:
                await self._redis.incrby(f"{self.prefix}{metric_name}", count)
                # Update local cache
                self._local_cache[metric_name] = self._local_cache.get(metric_name, 0) + count
            except Exception as e:
                logger.error(f"Spacescraper Telemetry Error: Write failed: {e}")
            
    async def record_job_status(self, success: bool):
        """High-level abstraction for recording overall job success/failure."""
        await self.increment("jobs_total")
        if success:
            await self.increment("jobs_success")
        else:
            await self.increment("jobs_failed")
        # Trigger internal threshold audit
        await self._check_alerts()

    async def get_metrics(self) -> Dict[str, int]:
        """Fetches a current snapshot of all system counters asynchronously."""
        if not self._redis:
            return {k: 0 for k in self.metric_keys}
        
        result = {}
        try:
            # Use pipeline for efficient multi-key fetch
            pipe = self._redis.pipeline()
            for key in self.metric_keys:
                pipe.get(f"{self.prefix}{key}")
            values = await pipe.execute()
            
            for key, val in zip(self.metric_keys, values):
                result[key] = int(val) if val else 0
        except Exception as e:
            logger.error(f"Spacescraper Telemetry Error: Fetch failed: {e}")
            result = {k: 0 for k in self.metric_keys}
        
        return result

    @property
    def metrics(self) -> Dict[str, int]:
        """Synchronous access to cached metrics (may be stale)."""
        # Return local cache or zeros if not available
        return {k: self._local_cache.get(k, 0) for k in self.metric_keys}

    async def get_success_rate(self) -> float:
        """Calculates current success percentage for the dashboard."""
        stats = await self.get_metrics()
        total = stats.get("jobs_total", 0)
        if total == 0:
            return 100.0
        return (stats.get("jobs_success", 0) / total) * 100.0

    async def _check_alerts(self):
        """
        Threshold Audit.
        Triggers external notifications if success rates fall below 
        enterprise SLA levels.
        """
        stats = await self.get_metrics()
        success_rate = await self.get_success_rate()
        
        # Audit criteria: At least 50 jobs processed and < 85% success
        if stats.get("jobs_total", 0) > 50 and success_rate < 85.0:
            await self.send_alert(f"Critical SLA Alert: Success rate diverged to {success_rate:.2f}%")

    async def send_alert(self, message: str):
        """Dispatches an emergency notification to all configured channels."""
        logger.error(f"🚨 Spacescraper ALERT: {message}")
        from src.infrastructure.notifications.notifier import notifier
        try:
            await notifier.notify(message, channel="alerts")
        except Exception as e:
            logger.debug(f"Alert dispatch suppressed: {e}")

    async def close(self):
        """Cleanly close the Redis connection."""
        if self._redis:
            await self._redis.close()

# Core Singleton Pattern for cluster-wide metrics access
metrics_tracker = ObservabilityMetrics()
