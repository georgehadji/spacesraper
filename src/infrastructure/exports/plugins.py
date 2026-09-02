# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Export System)
# Role: Delivers intelligence to webhooks and team channels.

import logging
from typing import List

from src.domain.models import ExtractedRecord
from src.infrastructure.exports.base_plugin import BaseExportPlugin
from src.infrastructure.http_client import internal_http

logger = logging.getLogger("Spacescraper.Export")

class WebhookExportPlugin(BaseExportPlugin):
    """Signals discovery events to external API gateways."""

    def __init__(self, endpoint_url: str):
        self.endpoint_url = endpoint_url

    async def deliver(self, records: list[ExtractedRecord]):
        if not records: return
        try:
            payload = {
                "count": len(records),
                "entities": [t.model_dump(mode="json") for t in records]
            }
            await internal_http.post(self.endpoint_url, json=payload)
            logger.info(f"Spacescraper Export: Dispatched {len(records)} items to webhook.")
        except Exception as e:
            logger.error(f"Webhook delivery failure: {e}")

class SlackExportPlugin(BaseExportPlugin):
    """Posts formatted discovery summaries to Slack channels."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    async def deliver(self, records: list[ExtractedRecord]):
        if not records: return
        # Group records into a single notification to avoid spamming
        blocks = [{"type": "header", "text": {"type": "plain_text", "text": "🔔 Spacescraper: New Intelligence Discovery"}}]

        for t in records[:5]: # Cap at 5 for Slack readability
            url = t.canonical_url or t.source_url
            title = t.data.get("title") or t.data.get("name") or t.record_type
            buyer = t.data.get("buyer", "Unknown")
            budget = t.data.get("estimated_budget", "N/A")
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*<{url}|{title}>*\n🏢 *Buyer:* {buyer} | 💰 *Budget:* {budget}"
                }
            })

        try:
            await internal_http.post(self.webhook_url, json={"blocks": blocks})
            logger.info("Spacescraper Export: Summary posted to Slack.")
        except Exception as e:
            logger.error(f"Slack delivery failure: {e}")
