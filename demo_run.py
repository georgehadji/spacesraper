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
from src.application.extraction_pipeline import DeterministicExtractionPipeline, ExtractionPipeline
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
        logger.error(f'Spacescraper Fault: missing sample page at {sample_path}')
        return 1

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
    pipeline = ExtractionPipeline()
    strategy = DeterministicExtractionPipeline()

    logger.info("Spacescraper: Dispatching to Deterministic Extraction Kernel...")
    result = await pipeline.process(payload, strategy)

    if not result.success:
        logger.error(f'Spacescraper Fault: {result.error}')
        await intel_tracker.close()
        return 1

    if not result.entities:
        logger.error('Spacescraper Fault: extraction kernel produced no entities.')
        await intel_tracker.close()
        return 1

    # 4. Hub Execution (Decoupled Side-Effects)
    post_processor = IntelligencePostProcessor(intel_tracker=intel_tracker)

    logger.info("Spacescraper: Delegating to Post-Processor Hub...")
    # Note: State Audit is for Opportunities; other entity types pass through untouched.
    status_counts, audited = await post_processor.run_state_audit(result.entities)

    # Only Opportunity entities take part in the state audit; ExtractedRecords pass through.
    logger.info(f"Spacescraper Audit: {status_counts} ({len(audited)} opportunities persisted).")
    logger.info(f"Spacescraper Demo Complete. Extracted {len(result.entities)} entities.")
    print("\n[SUCCESS] OPTION 1 ARCHITECTURE VALIDATED\n")

    await intel_tracker.close()
    return 0

if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
