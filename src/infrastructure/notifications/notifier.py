# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Alerting & Notifications)
# Role: Dispatches real-time alerts to Slack, Discord, and system logs.

import logging
import os

from src.infrastructure.http_client import internal_http

# Specialized logger for audit trails of notifications
logger = logging.getLogger("Spacescraper.Notifier")

class NotificationService:
    """
    Spacescraper Alerting Node.
    Orchestrates the dispatch of critical event messages (Price Alerts, 
    Success/Failure discovery, SLA breaches) to external webhooks.
    """
    
    def __init__(self):
        # Configuration mapping for external Slack/Discord integration
        self.slack_webhook = os.environ.get("SLACK_WEBHOOK_URL")
        self.discord_webhook = os.environ.get("DISCORD_WEBHOOK_URL")
        
    async def notify(self, message: str, channel: str = "general"):
        """
        Multicast Notification logic. 
        Ensures visibility across local logs and external dashboards.
        """
        # Phase 1: Local Integrity logging
        logger.info(f"Spacescraper NOTIFY [{channel}]: {message}")
        
        # Phase 2: Enterprise Slack Delivery
        if self.slack_webhook:
            try:
                # Reuse the shared singleton HTTP client
                client = await internal_http.get_client()
                await client.post(self.slack_webhook, json={"text": message})
            except Exception as e:
                logger.error(f"Spacescraper Slack Error: Message delivery failed: {e}")
                
        # Phase 3: Developer Discord Delivery
        if self.discord_webhook:
            try:
                client = await internal_http.get_client()
                await client.post(self.discord_webhook, json={"content": message})
            except Exception as e:
                logger.error(f"Spacescraper Discord Error: Message delivery failed: {e}")

# Global singleton for system-wide notification access
notifier = NotificationService()
