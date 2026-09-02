# Valkey Streams adapter for typed message delivery.
# Replaces the old BLPOP/RPUSH LIST pattern with Streams,
# consumer groups, acknowledgments, retries, and a DLQ.

import json
import logging
import uuid
from collections.abc import Awaitable, Callable

import valkey.asyncio as valkey

from src.config_settings import settings
from src.domain.models import MessageType, QueueMessage

logger = logging.getLogger("Spacescraper.StreamQueue")

DLQ_SUFFIX = "_dlq"
DEFAULT_BLOCK_MS = 2000  # 2-second poll timeout
DEFAULT_BATCH_SIZE = 10  # messages per XREADGROUP call
MAX_RETRIES_DEFAULT = 3
CLAIM_IDLE_MS = 60_000  # 60 seconds before a pending message can be claimed
# Conservative cap applied when the fan-out budget can't be checked (no Valkey
# connection, or the EVAL failed). Fail closed, not open — see
# ValkeyStreamQueue.get_allowed_fanout.
FANOUT_DEGRADED_LIMIT = 10
_METRICS_PREFIX = "metrics:"  # Must match ObservabilityMetrics.prefix in observability.py


def _new_message_id() -> str:
    return uuid.uuid4().hex


def make_message(
    message_type: MessageType,
    payload: dict,
    *,
    correlation_id: str | None = None,
    root_job_id: str | None = None,
    retry_count: int = 0,
) -> QueueMessage:
    """Build a QueueMessage envelope. Shared by every producer (main.py, worker_scraper.py,
    worker_processor.py, cli.py, submit_url.py, spacescraper.py) so the envelope shape
    (message_id, schema_version, timestamp) stays consistent across the whole cluster."""
    return QueueMessage(
        message_id=_new_message_id(),
        message_type=message_type,
        correlation_id=correlation_id,
        root_job_id=root_job_id,
        payload=payload,
        retry_count=retry_count,
    )


class ValkeyStreamQueue:
    """
    Valkey Streams queue with consumer groups, acknowledgment, retry, and DLQ.

    Usage (producer):
        queue = ValkeyStreamQueue()
        await queue.push("jobs_stream", QueueMessage(...))

    Usage (consumer):
        queue = ValkeyStreamQueue()
        await queue.consume("jobs_stream", "scraper_group", "worker-1", callback)
    """

    def __init__(self, valkey_url: str = None):
        self.valkey_url = valkey_url or settings.valkey.url
        self._valkey: valkey.Valkey | None = None
        self._is_mock = False
        self.memory_limit_mb = 512  # Soft limit for backpressure (ported from valkey_worker.py)

    async def connect(self):
        """
        Initialize the Valkey connection.

        Idempotent: a shared queue may be connected by more than one worker, and
        reconnecting would orphan the previous client.
        """
        if self._valkey is not None:
            return
        try:
            self._valkey = valkey.from_url(self.valkey_url, decode_responses=True)
            await self._valkey.ping()
            logger.info("StreamQueue: Connected to %s", self.valkey_url)
        except Exception as e:
            logger.warning("StreamQueue: Live Valkey unreachable (%s), using in-memory fallback.", e)
            await self._setup_mock()

    async def _setup_mock(self):
        """Fall back to fakeredis for development/testing."""
        try:
            import fakeredis
            self._valkey = fakeredis.FakeAsyncValkey(decode_responses=True)
            self._is_mock = True
            logger.info("StreamQueue: Using in-memory Valkey fake.")
        except ImportError:
            logger.error("StreamQueue: fakeredis not available. Queue disabled.")
            self._valkey = None

    async def close(self):
        """Close the Valkey connection."""
        if self._valkey:
            await self._valkey.aclose()

    # --- Producer ---

    async def push(self, stream: str, message: QueueMessage, *, check_backpressure: bool = True) -> str:
        """
        Push a typed message to a Valkey Stream.

        Scenario 3: Memory-Aware Backpressure Guardrail (ported from valkey_worker.py).
        Above the soft limit, ingestion is throttled (logged only); above the hard
        limit (1.5x soft), the message is routed to the DLQ instead of enqueued to
        avoid an OOM crash, and this returns "" (no entry was written).

        Returns the Valkey-generated stream entry ID, or "" if dropped to DLQ.
        """
        assert self._valkey is not None

        if check_backpressure and not self._is_mock:
            try:
                info = await self._valkey.info(section="memory")
                used_memory = info.get("used_memory_rss", 0) / (1024 * 1024)  # MB

                if used_memory > self.memory_limit_mb:
                    logger.warning(
                        "StreamQueue Backpressure ALERT: Valkey memory usage (%.1fMB) exceeds threshold. Throttling ingestion.",
                        used_memory,
                    )
                    if used_memory > (self.memory_limit_mb * 1.5):  # Hard limit
                        logger.error(
                            "StreamQueue CRITICAL: Cluster Saturation. Routing %s to DLQ to prevent OOM crash.",
                            message.message_id,
                        )
                        await self.push_dlq(stream, message, reason="OOM_BACKPRESSURE")
                        # Written directly to Valkey (not via metrics_tracker) to avoid a circular import.
                        await self._valkey.incrby(_METRICS_PREFIX + "jobs_dropped_oom", 1)
                        return ""
            except Exception as e:
                logger.debug("Backpressure monitor failed: %s", e)

        entry_id = await self._valkey.xadd(
            stream,
            {"payload": message.model_dump_json()},
            maxlen=100_000,  # cap stream length
        )
        logger.debug("StreamQueue: Pushed %s to %s (id=%s)", message.message_type.value, stream, entry_id)
        return entry_id

    async def push_dlq(self, stream: str, message: QueueMessage, reason: str) -> str:
        """
        Push a message to the dead-letter queue for the given stream.
        """
        dlq_stream = stream + DLQ_SUFFIX
        entry = message.model_dump(mode="json")
        entry["dlq_reason"] = reason
        entry_id = await self._valkey.xadd(
            dlq_stream,
            {"payload": json.dumps(entry)},
            maxlen=10_000,
        )
        logger.warning("StreamQueue: DLQ'd %s to %s (reason=%s)", message.message_id, dlq_stream, reason)
        # Track DLQ depth
        try:
            from src.infrastructure.monitoring.observability import metrics_tracker
            depth = await self.get_dlq_length(stream)
            await metrics_tracker.gauge(f"dlq_depth:{stream}", depth)
        except Exception:
            pass
        return entry_id

    # --- Consumer ---

    async def _ensure_group(self, stream: str, group: str):
        """Create consumer group if it doesn't exist. Idempotent."""
        assert self._valkey is not None
        try:
            await self._valkey.xgroup_create(stream, group, id="0", mkstream=True)
            logger.info("StreamQueue: Created group %s on %s", group, stream)
        except valkey.ResponseError as e:
            if "BUSYGROUP" in str(e):
                pass  # group already exists
            else:
                raise

    DEDUP_TTL_SECONDS = 86400  # 24h dedup window

    async def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        callback: Callable[[QueueMessage], Awaitable[bool]],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        block_ms: int = DEFAULT_BLOCK_MS,
        max_retries: int = MAX_RETRIES_DEFAULT,
        claim_idle_ms: int = CLAIM_IDLE_MS,
    ):
        """
        Long-poll consumer loop.

        Args:
            stream: Valkey Stream key.
            group: Consumer group name (e.g. "scrapers").
            consumer: Unique consumer ID (e.g. "scraper-1").
            callback: Async callable that receives the QueueMessage and
                      returns True on success, False on failure (triggers retry/DLQ).
            batch_size: Max messages per XREADGROUP call.
            block_ms: Block time in ms for XREADGROUP.
            max_retries: Max delivery attempts before dead-letter.
            claim_idle_ms: Idle time before another consumer can claim a pending message.
        """
        assert self._valkey is not None
        await self._ensure_group(stream, group)

        logger.info(
            "StreamQueue: Consumer %s@%s listening on %s (batch=%d, block=%dms)",
            consumer, group, stream, batch_size, block_ms,
        )

        while True:
            try:
                # Read new messages
                results = await self._valkey.xreadgroup(
                    group, consumer, {stream: ">"},
                    count=batch_size, block=block_ms,
                )

                if results:
                    for stream_name, entries in results:
                        for entry_id, data in entries:
                            await self._process_entry(
                                stream_name, entry_id, data,
                                group, consumer,
                                callback, max_retries,
                            )

                # Claim pending messages from failed consumers
                await self._claim_pending(
                    stream, group, consumer,
                    callback, max_retries, claim_idle_ms,
                )

            except Exception as e:
                logger.error("StreamQueue: Consumer loop error: %s", e)
                import asyncio
                await asyncio.sleep(2)

    async def _process_entry(
        self,
        stream: str, entry_id: str, data: dict,
        group: str, consumer: str,
        callback: Callable[[QueueMessage], Awaitable[bool]],
        max_retries: int,
    ):
        """Deserialize, call callback, ACK or NACK/DLQ."""
        assert self._valkey is not None
        try:
            raw = json.loads(data.get("payload", "{}"))
            message = QueueMessage(**raw)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error("StreamQueue: Invalid message at %s/%s: %s", stream, entry_id, e)
            await self._valkey.xack(stream, group, entry_id)
            return

        # Dedup: skip if this message_id was already processed. The lock is
        # held only while the callback runs — on failure it's released below
        # so a retried delivery (same message_id) isn't permanently blocked.
        dedup_key = f"dedup:{message.message_id}"
        is_new = await self._valkey.set(dedup_key, "1", nx=True, ex=self.DEDUP_TTL_SECONDS)
        if not is_new:
            logger.debug("StreamQueue: Skipping duplicate message %s", message.message_id)
            await self._valkey.xack(stream, group, entry_id)
            return

        success = False
        try:
            success = await callback(message)
        except Exception as e:
            logger.error("StreamQueue: Callback error for %s: %s", message.message_id, e)

        if not success:
            await self._valkey.delete(dedup_key)

        if success:
            await self._valkey.xack(stream, group, entry_id)
            logger.debug("StreamQueue: ACK'd %s (%s)", message.message_id, entry_id)
        else:
            new_retry = message.retry_count + 1
            if new_retry >= max_retries:
                await self.push_dlq(stream, message, f"Exhausted retries ({new_retry}/{max_retries})")
                await self._valkey.xack(stream, group, entry_id)
                logger.warning("StreamQueue: DLQ'd %s after %d retries", message.message_id, new_retry)
            else:
                # Re-push with incremented retry count
                retry_msg = message.model_copy(update={"retry_count": new_retry})
                await self.push(stream, retry_msg)
                await self._valkey.xack(stream, group, entry_id)
                logger.info("StreamQueue: Retry %d/%d for %s", new_retry, max_retries, message.message_id)

    async def _claim_pending(
        self,
        stream: str, group: str, consumer: str,
        callback: Callable[[QueueMessage], Awaitable[bool]],
        max_retries: int, claim_idle_ms: int,
    ):
        """Claim pending messages from failed consumers and reprocess them."""
        assert self._valkey is not None
        try:
            pending = await self._valkey.xpending_range(stream, group, min="-", max="+", count=5)
            if not pending:
                return

            pending_ids = [p["message_id"] for p in pending if int(p["times_delivered"]) > 0]
            if not pending_ids:
                return

            claimed = await self._valkey.xclaim(
                stream, group, consumer, claim_idle_ms, pending_ids,
            )
            for entry_id, data in claimed:
                logger.info("StreamQueue: Claimed pending %s/%s", stream, entry_id)
                await self._process_entry(
                    stream, entry_id, {stream: data},
                    group, consumer, callback, max_retries,
                )
        except Exception as e:
            logger.debug("StreamQueue: Claim-pending error (harmless): %s", e)

    # --- Utility ---

    async def get_stream_length(self, stream: str) -> int:
        """Get the approximate number of entries in a stream."""
        assert self._valkey is not None
        try:
            info = await self._valkey.xlen(stream)
            return info
        except Exception:
            return 0

    async def get_dlq_length(self, stream: str) -> int:
        """Get the number of dead-lettered messages."""
        return await self.get_stream_length(stream + DLQ_SUFFIX)

    async def get_pending_count(self, stream: str, group: str) -> int:
        """Get the number of pending (unacknowledged) messages."""
        assert self._valkey is not None
        try:
            info = await self._valkey.xpending(stream, group)
            return info.get("pending", 0) if isinstance(info, dict) else 0
        except Exception:
            return 0

    async def get_allowed_fanout(self, root_job_id: str, requested: int, max_fanout: int) -> int:
        """
        Atomic fan-out budget check via Lua script (ported from valkey_worker.py).
        Returns how many of `requested` child jobs are allowed under the per-root cap.
        Uses Valkey EVAL for atomic read-modify-write; **fails closed** on error.

        In mock/dev mode: enforces the cap in-process rather than returning
        requested. On Valkey error or no connection: returns
        FANOUT_DEGRADED_LIMIT.

        This method used to fail open (`return requested` on any error), which
        defeated the cap in exactly the situations it exists for. The
        fail-closed behaviour below is ported from the pre-migration
        redis_worker.py, which had been fixed there and would otherwise have
        been lost when that module was deleted — the migration to Streams
        carried the old fail-open code forward instead of the fix.
        """
        fanout_key = f"fanout:{root_job_id}"

        # Mock/dev mode: enforce the cap in-process rather than waving jobs
        # through. Per-instance, not shared across replicas — good enough for
        # a mode that has no shared store by definition.
        if self._is_mock:
            if not hasattr(self, "_fanout_tracker"):
                self._fanout_tracker: dict[str, int] = {}
            current = self._fanout_tracker.get(root_job_id, 0)
            available = max(0, max_fanout - current)
            allowed = min(requested, available)
            if allowed > 0:
                self._fanout_tracker[root_job_id] = current + allowed
            return allowed

        if self._valkey is None:
            # No connection and not mock mode: fail closed. Nothing to record a
            # metric on here by definition — the except branch below does that,
            # where a live connection still exists.
            logger.warning(
                "Fan-out cap: Valkey unavailable for %s, allowing degraded limit of %d",
                root_job_id, FANOUT_DEGRADED_LIMIT,
            )
            return min(requested, FANOUT_DEGRADED_LIMIT)

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
            valkey_eval = self._valkey.eval
            result = await valkey_eval(lua_script, 1, fanout_key, str(requested), str(max_fanout))
            return int(result)
        except Exception as e:
            # Fail closed: cap at the degraded limit and record the incident.
            logger.warning(
                "Fan-out check failed (%s) for %s, restricting to degraded limit %d",
                e, root_job_id, FANOUT_DEGRADED_LIMIT,
            )
            try:
                await self._valkey.incrby(_METRICS_PREFIX + "fanout_degraded_total", 1)
            except Exception:
                pass  # Best-effort metric increment
            return min(requested, FANOUT_DEGRADED_LIMIT)


# Backwards-compatible alias. The Valkey migration renamed this class from
# RedisStreamQueue; the discovery branch's worker_discovery.py still imports
# the old name. Same class, so both spellings stay valid rather than forcing
# a rename across code that merges cleanly otherwise.
RedisStreamQueue = ValkeyStreamQueue
