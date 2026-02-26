# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Unified Control Tower)
# Role: Single-entry point for the entire intelligence cluster.
# UX Improvement: Reduces cognitive load by orchestrating all nodes in one process.

import asyncio
import logging
import yaml
import sys
from typing import List

# Import worker nodes
from worker_scraper import ScraperWorkerService
from worker_processor import ProcessorWorkerService
from src.domain.models import ScrapeJob
from src.infrastructure.queues.redis_worker import RedisQueueWorker

import os
from src.domain.exceptions import SpacescraperError

# Styling the console
class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

from src.infrastructure.logger_config import setup_production_logging, Colors
from src.domain.exceptions import SpacescraperError

setup_production_logging()
logger = logging.getLogger("Spacescraper.Tower")

async def seed_jobs_from_config():
    """UX Improvement 2: Auto-Discovery & Zero-Config Seeding."""
    queue = RedisQueueWorker()
    try:
        with open("sources.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        
        sources = config.get("sources", [])
        logger.info(f"Spacescraper Tower: Analyzing registry for active missions...")
        
        for source in sources:
            if not source.get("enabled", True): continue
            
            for url in source.get("start_urls", []):
                job = ScrapeJob(
                    job_id=f"init_{source['target_site']}",
                    url=url,
                    target_site=source.get("target_site", "universal"), # Default to fallback
                    overlay=source.get("overlay") # Inject any declarative map
                )
                await queue.push_job("jobs_queue", job)
                logger.debug(f"Seeded job: {url}")
                
        logger.info(f"{Colors.OKGREEN}Spacescraper Tower: Seeding complete. All systems GO.{Colors.ENDC}")
    finally:
        await queue.close()

async def check_redis_status():
    """UX Improvement: Inform the user about the queue backend status."""
    queue = RedisQueueWorker()
    try:
        logger.info(f"{Colors.OKCYAN}Spacescraper Tower: Verifying message broker status...{Colors.ENDC}")
        await queue.connect()
        
        if queue._is_mock:
            print(f"{Colors.WARNING}[WARN] Live Redis not found. Running in OFFLINE mode (In-memory).{Colors.ENDC}")
            print(f"{Colors.WARNING}[WARN] Note: Data will be lost on exit and cluster nodes may not sync.{Colors.ENDC}\n")
        else:
            logger.info(f"{Colors.OKGREEN}Spacescraper Tower: Connected to Live Redis cluster.{Colors.ENDC}")
    except Exception as e:
        logger.error(f"Queue Check Failed: {e}")
    finally:
        await queue.close()

async def run_cluster():
    """
    Orchestrates the parallel execution of Scraper and Processor.
    """
    print(f"\n{Colors.BOLD}{Colors.OKBLUE}" + "="*60)
    print("    SPACESCRAPER ENTERPRISE INTELLIGENCE CLUSTER v2.5")
    print("="*60 + f"{Colors.ENDC}\n")

    # Step 1: Pre-flight checks
    await check_redis_status()
    await seed_jobs_from_config()

    # Step 2: Initialize Worker Nodes
    scraper = ScraperWorkerService()
    processor = ProcessorWorkerService()

    # Step 3: Concurrent Execution
    logger.info(f"{Colors.OKBLUE}Spacescraper Tower: Orchestrating Scraper and Processor nodes...{Colors.ENDC}")
    try:
        # We run both workers in the same event loop
        await asyncio.gather(
            scraper.run(),
            processor.run()
        )
    except KeyboardInterrupt:
        logger.info(f"{Colors.WARNING}Spacescraper Tower: Shutdown requested by operator.{Colors.ENDC}")
    except Exception as e:
        logger.error(f"{Colors.FAIL}Spacescraper Tower: Fatal Cluster Failure: {e}{Colors.ENDC}")

if __name__ == "__main__":
    try:
        asyncio.run(run_cluster())
    except KeyboardInterrupt:
        pass
