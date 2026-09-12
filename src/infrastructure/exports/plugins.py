# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Export System)
# Role: Delivers intelligence to webhooks and team channels.

import logging

from src.domain.models import ExtractedRecord
from src.infrastructure.exports.base_plugin import BaseExportPlugin
from src.infrastructure.http_client import internal_http

logger = logging.getLogger("Spacescraper.Export")


async def _post_or_raise(url: str, payload: dict, channel: str) -> None:
    """POST and treat a non-2xx as a failed delivery.

    httpx does not raise on 4xx/5xx, so both plugins used to discard the
    response and log success for a rejected POST. That also left
    worker_reporter's total-failure detection with nothing to detect: it
    gathers deliver() calls with return_exceptions=True, and deliver() had no
    failure mode that reached it. The exception is logged here and re-raised
    so the caller can decide whether the message was delivered at all.
    """
    try:
        response = await internal_http.post(url, json=payload)
    except Exception as e:
        logger.error(f"{channel} delivery failure: {e}")
        raise
    if response.status_code >= 400:
        detail = (response.text or "")[:200]
        logger.error(f"{channel} delivery rejected with {response.status_code}: {detail}")
        raise RuntimeError(f"{channel} delivery rejected with {response.status_code}: {detail}")

class WebhookExportPlugin(BaseExportPlugin):
    """Signals discovery events to external API gateways."""

    def __init__(self, endpoint_url: str):
        self.endpoint_url = endpoint_url

    async def deliver(self, records: list[ExtractedRecord]):
        if not records: return
        payload = {
            "count": len(records),
            "entities": [t.model_dump(mode="json") for t in records]
        }
        await _post_or_raise(self.endpoint_url, payload, "Webhook")
        logger.info(f"Spacescraper Export: Dispatched {len(records)} items to webhook.")

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

        await _post_or_raise(self.webhook_url, {"blocks": blocks}, "Slack")
        logger.info("Spacescraper Export: Summary posted to Slack.")
