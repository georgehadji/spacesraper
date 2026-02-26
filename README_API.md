# 🌐 Spacescraper API Reference

The Spacescraper Node Controller provides a RESTful interface for the distributed crawling cluster.

## 🛠 Quick Start
The API is available at `http://localhost:8000`. 
Access the interactive documentation at `/docs` (Swagger) or `/redoc` (Redoc).

---

## 🚀 Endpoints

### 1. `POST /jobs` - Submit Scraping Task
Enqueues a job for the workers.

**Payload Structure:**
```json
{
  "url": "https://ted.europa.eu",
  "target_site": "universal",
  "persona_id": "corporate_analyst_01",
  "webhook_url": "https://your-app.com/api/intel",
  "overlay": {
    "entity_type": "Tender",
    "container": ".tender-row",
    "mapping": {
      "title": "h2.title",
      "deadline": ".date-expires"
    }
  }
}
```

### 2. `GET /metrics` - Cluster Telemetry
Returns live performance statistics.
```json
{
  "jobs_total": 150,
  "jobs_success": 142,
  "pages_scraped": 450,
  "turbo_mode_hits": 28,
  "stealth_decay_events": 2
}
```

### 3. `GET /health` - System Audit
Verify the node is operational.

---

## 🔐 Security & Constraints
- **SSRF Shield**: All `webhook_url` inputs are filtered to prevent internal network scanning.
- **Validation**: Strict Pydantic validation on all incoming URLs and payloads.
- **Concurrency**: Asynchronous enqueuing via Redis (O(1) complexity).
