# Spacescraper

Async web scraping pipeline with a small FastAPI control surface.

## What remains

- Job submission via `POST /jobs`
- HTML-to-overlay generation via `POST /autograph`
- Metrics and health endpoints
- Redis-backed worker pipeline for scrape -> process -> report

## What was removed

- Procurement-specific product surface
- Win prediction endpoints and demos
- WooCommerce export path
- Duplicate dashboard variants

## Quick start

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Main endpoints

- `GET /health`
- `POST /jobs`
- `POST /autograph`
- `GET /metrics`

Protected endpoints require:

```text
Authorization: Bearer ss_your_api_key_here
```

## Notes

- The internal extraction pipeline still contains generic entity models and historical opportunity-oriented persistence code. This change removes those features from the active project surface rather than rewriting the whole pipeline in one pass.
