# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Dry Run)
# Role: Simulates a discovery event to verify the Reporter logic.

import asyncio
import os
import shutil
from datetime import datetime
from src.domain.models import ExtractedRecord, DiscoveryEvent
from worker_reporter import ReporterWorkerService

async def simulate_discovery():
    print("🧪 Spacescraper: Initiating Reporter Dry Run...")
    
    # 1. Cleanup old exports for clean validation
    if os.path.exists("exports"):
        print("🧹 Cleaning old exports...")
        # We don't delete evidence, just reports
        for f in os.listdir("exports"):
            if f.endswith((".xlsx", ".csv", ".html")):
                os.remove(os.path.join("exports", f))

    # 2. Create Mock Intelligence
    mock_records = [
        ExtractedRecord(
            record_id="dry_run_1",
            record_type="opportunity",
            source_url="https://business.esa.int/list",
            canonical_url="https://business.esa.int/dry-run-test",
            data={
                "title": "AI Orbit Optimization System",
                "buyer": "European Space Agency",
                "deadline": "2026-12-31",
                "estimated_budget": "2.500.000 €",
            },
        ),
        ExtractedRecord(
            record_id="dry_run_2",
            record_type="opportunity",
            source_url="https://business.esa.int/list",
            canonical_url="https://business.esa.int/quantum-test",
            data={
                "title": "Quantum Shielding Prototype",
                "buyer": "ESA - Science Dept",
                "deadline": "2027-01-15",
                "estimated_budget": "850.000 €",
            },
        ),
    ]

    event = DiscoveryEvent(
        job_id="dry_run_789",
        target_site="esa_emits",
        new_count=2,
        updated_count=0,
        entities=mock_records
    )

    # 3. Instantiate and Trigger Reporter
    reporter = ReporterWorkerService()
    print("📡 Dispatching simulated event to Reporter...")
    await reporter.handle_event(event)

    # 4. Validation
    print("\n🧐 Validation Audit:")
    files = os.listdir("exports")
    expected = ["pulse_preview.html"]
    
    excel_found = any(".xlsx" in f for f in files)
    csv_found = any(".csv" in f for f in files)
    html_found = "pulse_preview.html" in files

    if excel_found: print("✅ Excel Shipment Generated")
    if csv_found: print("✅ CSV Shipment Generated")
    if html_found: print("✅ Pulse Preview Dashboard Generated")

    if excel_found and csv_found and html_found:
        print("\n🚀 DRY RUN SUCCESSFUL: The Event-Driven Reporter is fully operational.")
    else:
        print("\n❌ DRY RUN FAILED: Some shipments were not generated.")

if __name__ == "__main__":
    asyncio.run(simulate_discovery())
