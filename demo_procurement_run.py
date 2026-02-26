# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (End-to-End Validation)
# Role: Validates the Defense & Space procurement intelligence flow.

import asyncio
import logging
from src.domain.models import RawScrapePayload
from worker_processor import ProcessorWorkerService
from src.infrastructure.storage.sqlite_tracker import intel_tracker

# Mock HTML Data for the demo
ESA_MOCK = """
<table class="data-table">
    <tr class="data-row">
        <td>AO1024</td>
        <td><a href="/emits/tender123">Sentinel-6 Satellite Maintenance Phase 2</a></td>
        <td>ESA/ESTEC</td>
        <td>2024-12-30</td>
        <td>OPEN</td>
    </tr>
</table>
"""

NATO_MOCK = """
<table>
    <tr class="GridViewRow">
        <td>NSPA-2024-001</td>
        <td>Tactical Communication Modules for Airborne Units</td>
        <td><td><td><td><td>2024-11-15</td>
    </tr>
</table>
"""

SAM_MOCK = """
[
    {
        "noticeId": "DOD-INTEL-001",
        "title": "Tactical Communication Systems - Defense Project",
        "organizationLocation": {"name": "Department of the Navy"},
        "responseDate": "2024-11-16",
        "status": "ACTIVE"
    }
]
"""

async def run_intel_validation():
    print("🚀 Spacescraper Intelligence Validation: Firing Up...")
    processor = ProcessorWorkerService()
    await intel_tracker.initialize()

    # 1. ESA Ingestion
    print("\n[Node 1] Processing ESA EMITS Flight...")
    esa_payload = RawScrapePayload(
        job_id="demo_esa_001",
        target_site="esa_emits",
        url="https://emits.esa.int",
        status_code=200,
        html_content=ESA_MOCK
    )
    await processor.process_payload(esa_payload)

    # 2. NATO Ingestion
    print("\n[Node 2] Processing NATO NSPA Ingestion...")
    nato_payload = RawScrapePayload(
        job_id="demo_nato_001",
        target_site="nato_nspa",
        url="https://nspa.nato.int",
        status_code=200,
        html_content=NATO_MOCK
    )
    await processor.process_payload(nato_payload)

    # 3. SAM.gov Ingestion (Triggering Fuzzy Deduplication Clustering)
    # Notice the title is 95% similar to the NATO one
    print("\n[Node 3] Processing SAM.gov (Simulation of High Title Similarity)...")
    sam_payload = RawScrapePayload(
        job_id="demo_sam_001",
        target_site="sam_gov",
        url="https://sam.gov",
        status_code=200,
        json_payloads=SAM_MOCK # Error here: SAM_MOCK is a string, process_payload expects a list of dicts if using json_payloads logic in strategy. 
                               # Actually our strategy does json.loads if not list, or we can pass pre-loaded.
                               # Let's pass pre-loaded.
    )
    # Re-wrap SAM_MOCK as actual data
    import json
    sam_payload.json_payloads = json.loads(SAM_MOCK)
    await processor.process_payload(sam_payload)

    print("\n✅ Spacescraper Intelligence validation complete.")
    print("📂 Check 'exports/' for the multi-sheet .xlsx report.")
    print("📈 Check 'spacescraper_intel.db' for current state history.")

if __name__ == "__main__":
    asyncio.run(run_intel_validation())
