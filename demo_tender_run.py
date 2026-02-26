# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Procurement Intelligence Demo)
# Role: Local validation of the Tender/RFP extraction and processing pipeline.

import asyncio
import os
import logging
from pathlib import Path
from datetime import datetime

# Domain and Application layers
from src.domain.models import RawScrapePayload
from src.application.pipeline import DataPipeline
from src.extractors.target_tender_generic import GenericTenderExtractionStrategy

# Professional logging configuration
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('Spacescraper.TenderDemo')

async def main():
    """
    Spacescraper Tender Validation Cycle.
    Demonstrates the extraction of public procurement data from unstructured HTML.
    1. Loads or generates a sample Tender listing.
    2. Executes the DataPipeline with the Tender Strategy.
    3. Displays extracted intelligence (Ref ID, Title, Buyer, Deadline).
    """
    
    # --- Sample Artifact Management ---
    sample_path = Path(__file__).parent / 'sample_tender.html'
    if not sample_path.is_file():
        # Provision a mock procurement portal if the artifact is missing
        logger.info("Spacescraper Demo: Generating mock procurement portal...")
        html_content = """
        <!DOCTYPE html>
        <html>
        <head><title>Space Logistics Procurement Portal</title></head>
        <body>
          <h2>Active Galactic Tenders</h2>
          <div class="procurement-list">
            <div class="procurement-card">
                <span class="reference">SS-2026-ALPHA</span>
                <h3><a href="/tenders/alpha">Cryogenic Fuel Supply for Mars Transit</a></h3>
                <div class="entity">SpaceX Global Logistics</div>
                <div class="deadline">2026-06-30</div>
            </div>
            <div class="procurement-card">
                <span class="reference">SS-2026-BETA</span>
                <h3><a href="/tenders/beta">Automated Drone Maintenance - Lunar Base 1</a></h3>
                <div class="entity">European Space Agency</div>
                <div class="deadline">2026-12-15</div>
            </div>
          </div>
        </body>
        </html>
        """
        sample_path.write_text(html_content, encoding='utf-8')
    else:
        html_content = sample_path.read_text(encoding='utf-8')

    # --- Phase 1: Payload Construction ---
    payload = RawScrapePayload(
        job_id='demo_tender_tx_123',
        target_site='tender_generic',
        url="https://procurement.spacescraper.ai/active",
        status_code=200,
        html_content=html_content,
        json_payloads=[],
    )

    # --- Phase 2: Pipeline Execution ---
    # The pipeline uses the specialized Tender Strategy to resolve procurement entities
    pipeline = DataPipeline(ai_enrichment_enabled=False)
    strategy = GenericTenderExtractionStrategy()
    
    logger.info("Spacescraper: Dispatching tender payload to the Analytic Pipeline...")
    result = await pipeline.process(payload, strategy)

    if not result.success:
        logger.error(f'Spacescraper Pipeline Fault: {result.error_message}')
        return

    # --- Phase 3: Intelligence Display ---
    logger.info("="*60)
    logger.info(f"   SPACESCRAPER INTELLIGENCE: {len(result.entities)} TENDERS DETECTED")
    logger.info("="*60)
    for t in result.entities:
        logger.info(f" > [{t.reference_id}] {t.title}")
        logger.info(f"   Buyer: {t.buyer or 'N/A'} | Deadline: {t.deadline or 'N/A'}")
        logger.info("-" * 60)
        
    print("\n" + "="*60)
    print("      SPACESCRAPER TENDER VALIDATION COMPLETE")
    print("="*60 + "\n")

if __name__ == '__main__':
    asyncio.run(main())
