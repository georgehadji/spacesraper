# 🚀 Spacescraper Enterprise Deployment Checklist

This document outlines the protocols for deploying the Spacescraper Intelligence Cluster to production environments.

---

## 1. 🛠 Infrastructure & Dependencies
- [ ] **Python Runtimes**: Ensure Python 3.9+ is installed.
- [ ] **Message Broker**: Live Valkey instance (v7.2+) accessible via TLS.
- [ ] **Browser Runtimes**: Install Playwright binaries:
  ```bash
  pip install -r requirements.txt
  playwright install chromium
  ```
- [ ] **Storage**: 
  - [ ] Write permissions for `exports/` directory.
  - [ ] Write permissions for `logs/` directory.
  - [ ] Persistent volume for `spacescraper_intel.db` (if using SQLite).

---

## 2. 🔐 Environment Configuration (`.env`)
Create a `.env` file in the root directory. **Never commit this file to VCS.**

| Variable | Description | Default |
| :--- | :--- | :--- |
| `VALKEY_URL` | Connection string for the task queue. | `valkey://localhost:6379` |
| `DB_PATH` | Path to the SQLite state database. | `spacescraper_intel.db` |
| `LOG_LEVEL` | Detail level (INFO/DEBUG/WARNING). | `INFO` |
| `API_PORT` | Port for the FastAPI Gateway. | `8000` |
| `SCRAPER_POOL_SIZE` | Parallel browser contexts per worker. | `2` |
| `STEALTH_MODE` | Toggle advanced evasion scripts. | `true` |

---

## 🏗 Deployment Strategy
### Step 1: Pre-flight Audit
Run the test suite to ensure kernel and hub integrity:
```bash
pytest tests/
```

### Step 2: Service Launch
Using the Unified Control Tower for small footprints:
```bash
python spacescraper.py
```
For enterprise-scale, launch nodes independently (PM2 or Docker):
```bash
# Node A: API Gateway
uvicorn main:app --host 0.0.0.0 --port 8000
# Node B: Scraper Cluster
python worker_scraper.py
# Node C: Intelligence Processor
python worker_processor.py
```

---

## 💾 Backup Strategy
1.  **State DB Snapshot**: Perform a `VACUUM INTO` on the SQLite DB every 24 hours.
    ```sql
    VACUUM INTO 'backups/intel_backup_YYYYMMDD.db';
    ```
2.  **Configuration**: Mirror `sources.yaml` to a secure private S3 bucket.
3.  **Logs**: Use `logrotate` for `logs/trace.log` to prevent disk exhaustion.

---

## 🔄 Rollback Plan (Disaster Recovery)
If `metrics:jobs_failed` exceeds 25% within the first hour of deployment:

1.  **Immediate Stop**: Terminate all active worker nodes.
2.  **VCS Revert**: Roll back the `git` HEAD to the last stable tag:
    ```bash
    git checkout tags/v2.4.stable
    ```
3.  **State Recovery**: If schema migration occurred, restore the last healthy `intel_backup.db`.
4.  **Flush Transient tasks**: Clear the Valkey queue to prevent old-version workers from picking up new-schema jobs.
    ```bash
    valkey-cli FLUSHDB
    ```

---

## 📊 Post-Deployment Monitoring (KPIs)
- [ ] Verify `pulse_preview.html` generation.
- [ ] Check `logs/trace.log` for `StealthViolation` errors.
- [ ] Monitor Valkey memory usage for queue saturation.
