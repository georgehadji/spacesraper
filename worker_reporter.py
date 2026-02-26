# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Event Consumer)
# Role: Subscribes to DISCOVERY_EVENT and executes side-effects (Slack, Reports, Webhooks).

import asyncio
import logging
import os
from src.infrastructure.queues.redis_worker import RedisQueueWorker
from src.domain.models import DiscoveryEvent
from src.infrastructure.exports.plugins import SlackExportPlugin, WebhookExportPlugin
from src.infrastructure.exports.report_generator import ReportGenerator
from src.infrastructure.http_client import http_client
from src.infrastructure.logger_config import setup_production_logging

setup_production_logging()
logger = logging.getLogger("Spacescraper.Reporter")

class ReporterWorkerService:
    """
    Spacescraper Event Orchestrator.
    Listens for intelligence signals and dispatches to all configured channels.
    """
    
    def __init__(self):
        self.queue = RedisQueueWorker()
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
        
        # 1. Generate local shipments (Excel/CSV/HTML)
        self.report_gen.generate_excel_csv(event.entities, event.target_site)
        self.report_gen.generate_pulse_dashboard(event.entities, event.target_site)
        
        # 2. Multi-channel delivery
        delivery_tasks = []
        for plugin in self.plugins:
            delivery_tasks.append(plugin.deliver(event.entities))
            
        if delivery_tasks:
            await asyncio.gather(*delivery_tasks, return_exceptions=True)

    async def run(self):
        """Main event loop."""
        logger.info("🚀 Spacescraper Intelligence Reporter standing by for discovery signals...")
        # Try to connect to Redis (falls back to fakeredis if unavailable)
        await self.queue.connect()
        try:
            await self.queue.poll_events("discovery_events_queue", self.handle_event)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            pass
        finally:
            await self.queue.close()
            await http_client.close()

if __name__ == "__main__":
    worker = ReporterWorkerService()
    asyncio.run(worker.run())
