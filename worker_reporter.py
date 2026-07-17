# Author: Spacescraper
# Project: Spacescraper (Event Consumer)
# Role: Subscribes to discovery events via Valkey Streams and executes side-effects.

import asyncio
import logging
import os
from src.infrastructure.queues.stream_queue import RedisStreamQueue
from src.domain.models import DiscoveryEvent, QueueMessage, MessageType, ExtractedRecord
from src.infrastructure.exports.plugins import SlackExportPlugin, WebhookExportPlugin
from src.infrastructure.exports.report_generator import ReportGenerator
from src.infrastructure.exports.artifact_writers import write_artifacts
from src.infrastructure.http_client import http_client
from src.infrastructure.logger_config import setup_production_logging

setup_production_logging()
logger = logging.getLogger("Spacescraper.Reporter")

class ReporterWorkerService:
    """
    Listens for discovery signals via Valkey Streams and dispatches
    to all configured channels.
    """

    def __init__(self):
        self.stream_queue = RedisStreamQueue()
        self.report_gen = ReportGenerator()

        # Configure delivery plugins
        self.plugins = []
        slack_url = os.environ.get("SLACK_WEBHOOK_URL")
        if slack_url:
            self.plugins.append(SlackExportPlugin(slack_url))
            logger.info("Reporter: Slack plugin active.")

    async def handle_event(self, event: DiscoveryEvent):
        """Dispatches event to all side-effect handlers."""
        logger.info(f"Reporter: Received SIGNAL {event.event_id} from {event.target_site} ({event.new_count} new items)")

        # 1. Generate local shipments (Excel/CSV/JSON)
        self.report_gen.generate_excel_csv(event.entities, event.target_site)

        # 1b. Generate generic artifact files from any ExtractedRecords
        generic_records = [e for e in event.entities if isinstance(e, ExtractedRecord)]
        if generic_records:
            await write_artifacts(
                generic_records,
                name_prefix=event.target_site,
                formats=["csv", "json"],
            )

        # 2. Multi-channel delivery
        delivery_tasks = []
        for plugin in self.plugins:
            delivery_tasks.append(plugin.deliver(event.entities))

        if delivery_tasks:
            await asyncio.gather(*delivery_tasks, return_exceptions=True)

    async def process_stream_message(self, message: QueueMessage) -> bool:
        """Callback for Valkey Stream consumer."""
        try:
            payload = message.payload
            data = payload.get("data", {})
            event = DiscoveryEvent(
                event_id=payload.get("aggregate_id", message.message_id),
                job_id=data.get("job_id", ""),
                target_site=data.get("target_site", "unknown"),
                new_count=data.get("new_count", 0),
                updated_count=data.get("updated_count", 0),
            )
            await self.handle_event(event)
            return True
        except Exception as e:
            logger.error("Stream message processing failed: %s", e)
            return False

    async def run(self):
        """Main event loop."""
        logger.info("🚀 Spacescraper Intelligence Reporter standing by for discovery signals...")
        await self.stream_queue.connect()
        try:
            await self.stream_queue.consume(
                "discovery_stream", "reporters", "reporter-1",
                self.process_stream_message,
            )
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            pass
        finally:
            await self.stream_queue.close()
            await http_client.close()

if __name__ == "__main__":
    worker = ReporterWorkerService()
    asyncio.run(worker.run())
