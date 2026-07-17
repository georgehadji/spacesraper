# OutboxRelay — polls pending outbox events and delivers them to Valkey Streams.
# Runs as a background task in the API process or as a standalone worker.

import asyncio
import json
import logging
from typing import Optional

from src.domain.models import OutboxEvent, QueueMessage, MessageType
from src.infrastructure.repositories.outbox_repository import SqliteOutboxRepository
from src.infrastructure.queues.stream_queue import RedisStreamQueue

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

# Hard-coded outbox event types that should not be re-dispatched
TERMINAL_EVENT_TYPES = {"job.completed", "job.failed", "job.cancelled"}


class OutboxRelay:
    """
    Background service that polls the outbox repository for pending events
    and delivers them to the appropriate Valkey Stream.
    """

    def __init__(
        self,
        outbox_repo: SqliteOutboxRepository,
        stream_queue: Optional[RedisStreamQueue] = None,
    ):
        self.repo = outbox_repo
        self.stream_queue = stream_queue or RedisStreamQueue()
        self._running = False

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
        """Continuous polling loop."""
        self._running = True
        logger.info("OutboxRelay: Polling every %.1fs", poll_interval)
        while self._running:
            try:
                count = await self.run_once()
                if count > 0:
                    logger.debug("OutboxRelay: Delivered %d events", count)
            except Exception as e:
                logger.error("OutboxRelay: Poll error: %s", e)
            await asyncio.sleep(poll_interval)

    async def _deliver_event(self, event: OutboxEvent) -> None:
        """Deliver a single outbox event to the correct stream."""
        stream = EVENT_TYPE_TO_STREAM.get(event.event_type, "events_stream")
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
        except Exception as e:
            logger.warning("OutboxRelay: Failed to deliver %s: %s", event.event_id, e)
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
        repo: SqliteOutboxRepository,
        aggregate_type: str, aggregate_id: str, event_type: str,
        payload: dict,
    ) -> OutboxEvent:
        """Convenience method to create and persist an outbox event."""
        import uuid
        event = OutboxEvent(
            event_id=f"out_{uuid.uuid4().hex[:16]}",
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
        )
        await repo.create_event(event)
        return event
