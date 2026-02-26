# Spacescraper: Space & Defense Intelligence 플랫폼
**Author: Georgios-Chrysovalantis Chatzivantsidis**

Spacescraper is a production-grade web intelligence system specifically engineered for scraping, normalizing, and tracking **Space & Defense procurement tenders** from global portals like ESA, NATO, and SAM.gov.

## 🏛️ System Architecture

Spacescraper follows a **Hexagonal / Clean Architecture** pattern:

1.  **Scraper Layer**: Modular adapters (`target_*.py`) implementing `BaseExtractionStrategy`.
2.  **Orchestration Layer**: Redis-backed async workers decoupling ingestion from processing.
3.  **Intelligence Engine**: 
    *   **Fuzzy Deduplication**: Clusters tenders based on 90% title similarity.
    *   **State Auditing**: SQLite-backed history tracking with content hashing (NEW vs UPDATED).
    *   **Heuristic Classification**: Automated tagging of tenders into Space, Defense, or Dual-use categories.
4.  **Delivery Layer**: 
    *   **Intel Dashboard**: Real-time management console (FastAPI + React/Ant Design v5).
    *   **Multi-Sheet Export**: Microsoft Excel (.xlsx) generated with segregated sheets (All, New, Updated).

## 🚀 Deployment Instructions

### 1. Provision the Cluster (Docker)
Ensure Docker and Docker Compose are installed.
```bash
docker-compose up -d --build
```
This launches:
- `ss-broker`: Redis for messaging.
- `ss-ops`: Dashboard (accessible at http://localhost:8000).
- `ss-gateway`: REST API for manual job submission (http://localhost:8080).
- `ss-scraper-node`: Browser-based extraction farm.
- `ss-processor-node`: Intelligence engine.

### 2. Run Local Validation
To verify the system end-to-end with mock data:
```bash
python demo_procurement_run.py
```

## 🛠️ Configuration
Source registration is managed via `sources.yaml`. New sources can be added without code changes:
```yaml
  - name: "My New Portal"
    target_site: "custom_site"
    enabled: true
    interval_minutes: 60
    start_urls: ["https://example.com/tenders"]
```

## 📊 Data Assets
Generated files are stored in the `./exports` directory:
- `*_intel_*.xlsx`: Multi-sheet procurement reports.
- `*_intel_*.csv`: Flat data snapshots.
- `spacescraper_intel.db`: SQLite database containing historical run history.

---
*Spacescraper - Data Orchestration for the Modern Defense Enterprise.*
