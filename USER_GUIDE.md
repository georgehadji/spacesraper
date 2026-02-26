# Spacescraper: Operation & Deployment Guide
**Author: Georgios-Chrysovalantis Chatzivantsidis**

Welcome to Spacescraper, an enterprise-grade web intelligence platform. This guide provides the tactical instructions required to deploy, monitor, and scale the cluster.

---

## 📋 Prerequisites
- **Docker Desktop** (Engine 24.0+) installed and operational.
- **Python 3.12+** (required only for local CLI utilities).
- **OpenAI API Key** (Optional: required for AI Enrichment and Self-Healing).

---

## 🚀 Quick Start (Production-Ready)

### 1. Provision the Cluster
Build and launch the entire infrastructure including the Message Broker, Dashboard, API Gateway, and specialized Worker nodes.
```bash
docker-compose up -d --build
```
*Components started: Redis (SS-Broker), Dashboard (SS-Ops), API Node (SS-Gateway), Scraper Node, Processor Node.*

### 2. Access the Command Center
Open your browser to the SpacescraperOps Dashboard:
[http://localhost:8000](http://localhost:8000)

---

## 🛠️ Usage Patterns

### A. Manual Job Injection (CLI)
Use the `submit_url.py` utility to push ad-hoc targets into the active processing queue.
```bash
# Example: Submit an Amazon product for processing
python submit_url.py "https://www.amazon.com/dp/B07ZPKN6CH" --site amazon
```

### B. Scalable Submission (REST API)
The Node Controller provides a high-throughput REST interface at `http://localhost:8080`.
- **Swagger Documentation**: [http://localhost:8080/docs](http://localhost:8080/docs)
- **POST /jobs**:
  ```json
  {
    "url": "https://ek.gr/Product/12345",
    "target_site": "ek_gr"
  }
  ```

### C. Automated Catalog Monitoring
Spacescraper features a built-in scheduler (Orchestration Panel in the Dashboard).
1. Navigate to the **Dashboard**.
2. Add your **Target Sites** and **Entrypoint URLs** to the Registry.
3. Enable the **Dispatcher Switch** and set your desired re-scrape interval (e.g., 60 minutes).

---

## 📈 Scalability & Performance

### Horizontal Scaling
To increase throughput for massive data harvest campaigns, simply scale the scraper nodes:
```bash
# Scale to 5 concurrent browser workers
docker-compose up -d --scale scraper_node=5
```

---

## 📁 Data Assets & Intelligence
All processed shipments are stored in the `./exports` volume.
- **`*_raw_*.csv`**: Full extraction data for analysis.
- **`*_woo_export_*.csv`**: Formatted specifically for **WooCommerce** 'Product Importer'.
- **`*_tenders_*.xlsx`**: Multi-sheet procurement reports (New vs Updated).
- **`downloads/images/`**: Local cache of product imagery for storefront ingestion.

---

## 📝 Configuration Framework

### Site Strategies
Site-specific logic is housed in `src/extractors/`. To add support for a new portal:
1. Create `target_mysite.py` implementing `BaseExtractionStrategy`.
2. Register the strategy in `worker_processor.py`.

### AI Enrichment
To enable SEO generation and Self-Healing:
1. Add your `OPENAI_API_KEY` to the `.env` file.
2. The `DataPipeline` will automatically authorize GPT-4o-mini calls for new products.

---

## 🔍 Forensic Monitoring
Monitor cluster-wide logs in real-time through the Dashboard or the Docker daemon:
```bash
# Monitor the Scraper Node behavior
docker-compose logs -f scraper_node

# Monitor the Processor & AI Enrichment output
docker-compose logs -f processor_node
```

---
*Spacescraper - Data Orchestration for the Modern Enterprise.*
