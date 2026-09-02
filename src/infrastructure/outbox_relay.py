# OutboxRelay — polls pending outbox events and delivers them to Valkey Streams.
# Runs as a background task in the API process or as a standalone worker.

import asyncio
import logging
import random

from src.domain.models import MessageType, OutboxEvent, QueueMessage
from src.domain.ports import OutboxRepository
from src.infrastructure.queues.stream_queue import ValkeyStreamQueue

logger = logging.getLogger("Spacescraper.OutboxRelay")

# Map of aggregate event types to stream names
EVENT_TYPE_TO_STREAM = {
    "job.submitted": "jobs_stream",
    "job.completed": "jobs_stream",
    "job.failed": "jobs_stream",
    "job.cancelled": "jobs_stream",
    "discovery.new": "discovery_stream",
    "discovery.updated": "discovery_stream",
}

DEFAULT_POLL_INTERVAL = 2.0  # seconds between polls
DEFAULT_BATCH_SIZE = 20
MAX_CONSECUTIVE_FAILURES = 5  # circuit breaker trips after this many consecutive failures
CIRCUIT_BREAKER_PAUSE = 30.0  # seconds to pause relay when circuit breaker trips

# Hard-coded outbox event types that should not be re-dispatched
TERMINAL_EVENT_TYPES = {"job.completed", "job.failed", "job.cancelled"}


class OutboxRelay:
    """
    Background service that polls the outbox repository for pending events
    and delivers them to the appropriate Valkey Stream.
    """

    def __init__(
        self,
        outbox_repo: OutboxRepository,
        stream_queue: ValkeyStreamQueue | None = None,
    ):
        self.repo = outbox_repo
        self.stream_queue = stream_queue or ValkeyStreamQueue()
        self._running = False
        self._consecutive_failures = 0
        self._retry_base_delay = 1.0  # seconds; exponential backoff base

    async def start(self):
        """Initialize connections."""
        await self.stream_queue.connect()
        logger.info("OutboxRelay: Started")

    async def stop(self):
        """Close connections."""
        await self.stream_queue.close()
        logger.info("OutboxRelay: Stopped")

    async def run_once(self, batch_size: int = DEFAULT_BATCH_SIZE) -> int:
        """
        Poll pending events and deliver them. Returns count of events processed.
        Can be called periodically or in a loop.
        """
        events = await self.repo.get_pending_events(limit=batch_size)
        if not events:
            return 0

        for event in events:
            await self._deliver_event(event)

        return len(events)

    async def run_forever(self, poll_interval: float = DEFAULT_POLL_INTERVAL):
        """Continuous polling loop with circuit breaker and exponential backoff."""
        self._running = True
        logger.info("OutboxRelay: Polling every %.1fs", poll_interval)
        while self._running:
            try:
                count = await self.run_once()
                if count > 0:
                    logger.debug("OutboxRelay: Delivered %d events", count)
                    self._consecutive_failures = 0  # reset on success
            except Exception as e:
                self._consecutive_failures += 1
                logger.error("OutboxRelay: Poll error (%d consecutive): %s", self._consecutive_failures, e)
                # Circuit breaker
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.warning("OutboxRelay: Circuit breaker open, pausing %.0fs", CIRCUIT_BREAKER_PAUSE)
                    await asyncio.sleep(CIRCUIT_BREAKER_PAUSE)
                    self._consecutive_failures = 0
            await asyncio.sleep(poll_interval)

    async def _deliver_event(self, event: OutboxEvent) -> None:
        """Deliver a single outbox event to the correct stream with retry."""
        stream = EVENT_TYPE_TO_STREAM.get(event.event_type, "events_stream")
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                message = QueueMessage(
                    message_id=event.event_id,
                    message_type=self._infer_message_type(event.event_type),
                    correlation_id=event.aggregate_id,
                    root_job_id=event.aggregate_id if event.aggregate_type == "job" else None,
                    payload={
                        "event_type": event.event_type,
                        "aggregate_type": event.aggregate_type,
                        "aggregate_id": event.aggregate_id,
                        "data": event.payload,
                    },
                )
                await self.stream_queue.push(stream, message)
                await self.repo.mark_delivered(event.event_id)
                logger.debug("OutboxRelay: Delivered %s -> %s", event.event_id, stream)
                return
            except Exception as e:
                delay = self._retry_base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                logger.warning(
                    "OutboxRelay: Failed to deliver %s (attempt %d/%d): %s. Retrying in %.1fs",
                    event.event_id, attempt + 1, max_attempts, e, delay,
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(delay)
                else:
                    await self.repo.mark_failed(event.event_id, str(e))

    @staticmethod
    def _infer_message_type(event_type: str) -> MessageType:
        if event_type.startswith("job."):
            return MessageType.SCRAPE_JOB
        if event_type.startswith("discovery."):
            return MessageType.DISCOVERY_EVENT
        return MessageType.SCRAPE_JOB

    @staticmethod
    async def create_outbox_event(
        repo: OutboxRepository,
        aggregate_type: str, aggregate_id: str, event_type: str,
        payload: dict,
        *, conn=None,
    ) -> OutboxEvent:
        """Convenience method to create and persist an outbox event.

        conn forwards to OutboxRepository.create_event, letting a caller
        include this write in JobRepository.transaction()'s transaction
        (F14) instead of auto-committing on its own.
        """
        import uuid
        event = OutboxEvent(
            event_id=f"out_{uuid.uuid4().hex[:16]}",
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
        )
        await repo.create_event(event, conn=conn)
        return event
