# User Guide

## Run the API

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
python main.py
```

## Submit a job

Use `GET /demo/key` to retrieve the development token, then call:

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Authorization: Bearer ss_demo_key" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"https://example.com\",\"target_site\":\"universal\"}"
```
