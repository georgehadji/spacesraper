# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Orchestration Utility)
# Role: CLI tool for manual job injection into the Spacescraper cluster.

import argparse
import asyncio
import os
import uuid

from src.domain.models import MessageType, ScrapeJob
from src.infrastructure.queues.stream_queue import ValkeyStreamQueue, make_message


async def submit_single_url(url: str, target_site: str):
    """
    Manually injects a single URL into the global scraping pipeline.

    This utility connects to the shared Valkey infrastructure, serializes
    a ScrapeJob request, and publishes it to the 'jobs_stream'.
    It is the primary tool for testing new strategies or ad-hoc crawls.
    """
    # Configuration Discovery: Load Valkey cluster endpoint
    valkey_url = os.environ.get("VALKEY_URL", "valkey://localhost:6379")
    queue = ValkeyStreamQueue(valkey_url=valkey_url)
    
    # Generate unique traceability identifier
    job_id = f"man_ss_{uuid.uuid4().hex[:6]}"
    
    # Interface with Domain Models
    job = ScrapeJob(
        job_id=job_id,
        url=url,
        target_site=target_site
    )
    
    try:
        # Establish link to the queuing cluster
        await queue.connect()

        if queue._is_mock:
            # The offline fallback is a private in-memory queue. Reporting the job
            # as queued here would promise a pickup that can never happen.
            print("❌ Spacescraper Fault: No live broker reachable.")
            print("   The job would go to a private in-memory queue no worker can read.")
            print("   Maintenance Tip: Start Valkey (e.g., docker compose up -d valkey),")
            print("   or run a single scrape with: python cli.py scrape <url>")
            return 1

        # Publish the intent to the worker pool
        await queue.push(
            "jobs_stream",
            make_message(MessageType.SCRAPE_JOB, job.model_dump(mode="json"), root_job_id=job_id),
        )

        print("\n" + "="*50)
        print("🚀 Spacescraper: Job Authorized & Queued")
        print("="*50)
        print(f"   ID:     {job_id}")
        print(f"   Site:   {target_site}")
        print(f"   URL:    {url}")
        print("-" * 50)
        print("Status: Pending pickup by active scraper nodes.")
        print("="*50 + "\n")
        return 0

    except Exception as e:
        print(f"❌ Spacescraper Fault: Submission failed: {e}")
        print("Maintenance Tip: Verify Valkey connectivity (e.g., docker ps)")
        return 1
    finally:
        # Resource Teardown
        await queue.close()

if __name__ == "__main__":
    # CLI Argument Parsing
    parser = argparse.ArgumentParser(
        description="Submit custom URLs to the Spacescraper Enterprise Cluster.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("url", help="Target URL (including protocol)")
    parser.add_argument("--site", default="generic", help="Extraction strategy identifier")
    
    args = parser.parse_args()
    
    # Execute async session
    raise SystemExit(asyncio.run(submit_single_url(args.url, args.site)))
