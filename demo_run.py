# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Local Validation Utility)
# Role: End-to-end demonstration of the OPTION 1 Refactored Architecture.

import asyncio
import os
import logging
from pathlib import Path
from datetime import datetime

# Domain and Application layers
from src.domain.models import RawScrapePayload
from src.application.pipeline import DataPipeline
from src.extractors.universal_strategy import UniversalExtractionStrategy
from src.application.post_processor import IntelligencePostProcessor
from src.infrastructure.storage.sqlite_tracker import intel_tracker

# Professional logging configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('Spacescraper.Demo')

async def main():
    """
    Spacescraper Refactored Flow Validation.
    """
    # 0. Initialize Persistence
    await intel_tracker.initialize()
    
    # 1. Setup Sample Data
    sample_path = Path(__file__).parent / 'sample_product.html'
    if not sample_path.is_file():
        with open(sample_path, 'w', encoding='utf-8') as f:
            f.write('<div class="product"><h1>Spacescraper Pro</h1><span class="price">€99</span></div>')

    html_content = sample_path.read_text(encoding='utf-8')

    # 2. Payload Preparation
    payload = RawScrapePayload(
        job_id='demo_tx_opt1',
        target_site='universal',
        url="https://demo.spacescraper.ai",
        status_code=200,
        html_content=html_content,
        json_payloads=[],
        depth=0
    )

    # 3. Kernel Execution (Pure Extraction)
    pipeline = DataPipeline(ai_enrichment_enabled=False)
    strategy = UniversalExtractionStrategy()
    
    logger.info("Spacescraper: Dispatching to Universal Extraction Kernel...")
    result = await pipeline.process(payload, strategy)

    if not result.success:
        logger.error(f'Spacescraper Fault: {result.error}')
        return

    # 4. Hub Execution (Decoupled Side-Effects)
    post_processor = IntelligencePostProcessor()
    
    logger.info("Spacescraper: Delegating to Post-Processor Hub...")
    # Note: State Audit is for Tenders; Products use basic reporting
    status_counts = await post_processor.run_state_audit(result.entities)
    post_processor.generate_reports(result, payload.target_site)

    logger.info(f"Spacescraper Demo Complete. Found {len(result.entities)} entities.")
    print("\n[SUCCESS] OPTION 1 ARCHITECTURE VALIDATED\n")

if __name__ == '__main__':
    asyncio.run(main())
