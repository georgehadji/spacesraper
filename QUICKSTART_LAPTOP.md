# Spacescraper - Οδηγός Εκκίνησης στο Laptop

## 🚀 Γρήγορο Ξεκίνημα (5 λεπτά)

### Βήμα 1: Προαπαιτούμενα

```bash
# Έλεγξε ότι έχεις Python 3.9+
python --version  # Ή python3 --version

# Αν δεν έχεις Python, κατέβασε από:
# https://www.python.org/downloads/
```

### Βήμα 2: Κλωνοποίηση & Εγκατάσταση

```bash
# Μπες στο φάκελο του project
cd E:\Documents\Vibe-Coding\Scraper

# Δημιούργησε virtual environment (προαιρετικό αλλά συνιστάται)
python -m venv venv

# Ενεργοποίησε το (Windows PowerShell)
.\venv\Scripts\Activate.ps1

# Εγκατάσταση εξαρτήσεων
pip install -r requirements.txt
```

### Βήμα 3: Εκκίνηση Redis (Cache/Queue)

**Επιλογή Α: Με Docker (Ευκολότερο)**
```bash
# Αν έχεις Docker εγκατεστημένο
docker run -d -p 6379:6379 --name redis redis:7-alpine
```

**Επιλογή Β: Χωρίς Docker (Windows)**
```bash
# Κατέβασε Redis για Windows από:
# https://github.com/microsoftarchive/redis/releases

# Ή χρησιμοποίησε το Memurai (Redis για Windows):
# https://www.memurai.com/

# Εκκίνηση Redis
redis-server
```

**Επιλογή Γ: Fakeredis (Για testing - χωρίς Redis)**
```bash
# Δεν χρειάζεται να κάνεις τίποτα!
# Το project χρησιμοποιεί αυτόματα fakeredis αν δεν βρει Redis
```

### Βήμα 4: Εκκίνηση API Server

```bash
# Άνοιξε νέο terminal/PowerShell
# Μπες στο φάκελο του project
cd E:\Documents\Vibe-Coding\Scraper

# Ενεργοποίησε virtual environment (αν το έφτιαξες)
.\venv\Scripts\Activate.ps1

# Εκκίνηση API
python main.py
```

Θα δεις:
```
🚀 Spacescraper API Gateway is initializing...
INFO:     Started server process [12345]
INFO:     Uvicorn running on http://0.0.0.0:8000
```

**API είναι live στο:** http://localhost:8000

**Swagger Docs:** http://localhost:8000/docs

---

## 🎨 Εκκίνηση Dashboard (UI)

### Επιλογή 1: Streamlit Dashboard (Συνιστάται)

```bash
# Άνοιξε ΝΕΟ terminal (κράτησε ανοιχτό το API)
cd E:\Documents\Vibe-Coding\Scraper

# Ενεργοποίησε virtual environment
.\venv\Scripts\Activate.ps1

# Εκκίνηση UI
streamlit run dashboard.py
```

Θα ανοίξει αυτόματα στο browser: http://localhost:8501

### Επιλογή 2: Mobile Dashboard

```bash
streamlit run dashboard_mobile.py
```

### Επιλογή 3: Accessible Dashboard

```bash
streamlit run dashboard_accessible.py
```

---

## 🤖 Εκκίνηση Workers (Προαιρετικό)

Αν θες να επεξεργάζεσαι tenders αυτόματα:

```bash
# Terminal 1: Scraper Worker
python worker_scraper.py

# Terminal 2: Processor Worker  
python worker_processor.py

# Terminal 3: Reporter Worker
python worker_reporter.py
```

---

## ✅ Έλεγχος ότι Δουλεύει

### 1. Έλεγξε το API
```bash
# Άνοιξε browser και πήγαινε σε:
http://localhost:8000/health

# Θα δεις:
{
  "status": "healthy",
  "project": "Spacescraper",
  "version": "2.0.0"
}
```

### 2. Φτιάξε API Key
```bash
# POST request για νέο API key
# Με curl:
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "test@test.com", "tier": "pro"}'

# Ή μέσω Swagger UI:
# http://localhost:8000/docs → /auth/register → Try it out
```

### 3. Δες τα Metrics
```bash
# GET request
http://localhost:8000/metrics

# Θέλει API key στο header:
# Authorization: Bearer ss_demo_key
```

---

## 🖥️ Πλήρης Ρύθμιση (Όλα τα Services)

### 1. PostgreSQL (Αν θες αντί για SQLite)

```bash
# Με Docker
docker run -d \
  --name postgres \
  -e POSTGRES_PASSWORD=postgres \
  -p 5432:5432 \
  postgres:16-alpine

# Δημιούργησε .env αρχείο:
echo "DB_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/spacescraper" > .env
```

### 2. Playwright (Για scraping)

```bash
# Εγκατάσταση browsers
playwright install chromium
```

### 3. Environment Variables (.env αρχείο)

Δημιούργησε αρχείο `.env` στο root:

```env
# Database
DB_URL=sqlite:///spacescraper_intel.db
# Ή για PostgreSQL:
# DB_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/spacescraper

# Redis
REDIS_URL=redis://localhost:6379/0

# AI
GEMINI_API_KEY=your_key_here

# Features
FEATURE_KAFKA_EVENTS=false
FEATURE_POSTGRES_DB=false
FEATURE_OTEL_TRACING=false
```

---

## 🚀 One-Command Start (PowerShell Script)

Δημιούργησε αρχείο `start_all.ps1`:

```powershell
# Start_all.ps1
Write-Host "🚀 Starting Spacescraper..." -ForegroundColor Green

# Start Redis (if not running)
$redis = docker ps | Select-String "redis"
if (-not $redis) {
    Write-Host "Starting Redis..."
    docker run -d -p 6379:6379 --name redis redis:7-alpine 2>$null
}

# Start API (background job)
Write-Host "Starting API..."
Start-Process python -ArgumentList "main.py" -WindowStyle Hidden

# Wait for API
Start-Sleep -Seconds 3

# Start Dashboard
Write-Host "Starting Dashboard..."
streamlit run dashboard.py
```

Τρέξε το:
```powershell
.\start_all.ps1
```

---

## 🐛 Αντιμετώπιση Προβλημάτων

### Πρόβλημα: "Module not found"
```bash
# Λύση: Εγκατάσταση dependencies
pip install -r requirements.txt
```

### Πρόβλημα: "Port 8000 already in use"
```bash
# Βρες ποιο process χρησιμοποιεί την πόρτα
netstat -ano | findstr :8000

# Σκότωσέ το (ή άλλαξε πόρτα στο main.py)
taskkill /PID <PID> /F
```

### Πρόβλημα: "Redis connection refused"
```bash
# Το Redis δεν τρέχει - χρησιμοποίησε fakeredis
# Ή ξεκίνα Redis:
docker run -d -p 6379:6379 redis:7-alpine
```

### Πρόβλημα: "Permission denied" στο PowerShell
```powershell
# Άνοιξε PowerShell ως Administrator
# Ή τρέξε:
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

---

## 📱 Quick Test

Αφού ξεκινήσουν όλα:

1. **API:** http://localhost:8000/docs
2. **Dashboard:** http://localhost:8501
3. **Health Check:** http://localhost:8000/health

Δοκίμασε:
```bash
# Δημιούργησε API key
curl http://localhost:8000/auth/register \
  -d '{"email":"test@test.com","tier":"pro"}'

# Πάρε tenders
curl http://localhost:8000/tenders/high-quality \
  -H "Authorization: Bearer ss_demo_key"
```

---

## 🎯 Τι Τρέχει Πού

| Service | URL | Purpose |
|---------|-----|---------|
| API | http://localhost:8000 | Backend API |
| Swagger | http://localhost:8000/docs | API Documentation |
| Dashboard | http://localhost:8501 | Web UI |
| Redis | localhost:6379 | Cache/Queue |
| PostgreSQL | localhost:5432 | Database (optional) |

---

## 🛑 Τερματισμός

```bash
# Terminal με API: Ctrl+C
# Terminal με Dashboard: Ctrl+C

# Αν χρησιμοποιείς Docker:
docker stop redis
docker stop postgres  # αν τρέχει
```

---

**🎉 Είσαι έτοιμος! Το Spacescraper τρέχει στο laptop σου!**