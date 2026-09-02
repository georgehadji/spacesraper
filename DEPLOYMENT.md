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
| `SCRAPER_DISABLE_SANDBOX` | Force Chromium's `--no-sandbox` on (`true`) or off (`false`). Unset: auto-detected from the container runtime (Docker/Kubernetes cgroups). | auto-detect |
| `SCRAPER_FORENSIC_SCREENSHOTS` | Capture a full-page screenshot to `exports/evidence/` on scrape failure. May contain target-page personal data — debug only. | `false` |

### 🛡 Chromium Sandbox (SEC-5)
The sandbox is Chromium's primary containment boundary between hostile page
content and the host process — every worker navigates to arbitrary,
untrusted URLs, so this matters. It is **on by default** everywhere,
including containers.

Most container runtimes cannot run the sandbox without extra privileges
(`--cap-add=SYS_ADMIN` or a custom seccomp profile), so the pool
auto-disables it when it detects it's running inside a container
(`/.dockerenv` or a Docker/Kubernetes cgroup). Override explicitly with
`SCRAPER_DISABLE_SANDBOX=true` if detection misses your runtime, or `=false`
to force it on (e.g. if you've granted the container the extra privileges
instead).

**Residual risk when disabled:** a Chromium renderer compromised via a
malicious page has direct access to the container's filesystem and network
namespace, not just an isolated sandbox process. Mitigate with normal
container hardening — non-root user, read-only filesystem, minimal network
egress — rather than relying on the browser's own sandbox.

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
4.  **Forensic evidence** (SEC-6): screenshots in `exports/evidence/` can
    contain personal data from the target page and are **off by default** —
    set `SCRAPER_FORENSIC_SCREENSHOTS=true` only to debug a specific
    failure, never as a standing production setting. If enabled, schedule
    `src.infrastructure.storage.evidence_retention.purge_expired_evidence()`
    (default 7-day retention) the same way `logrotate` is scheduled above —
    nothing in this codebase invokes it automatically.

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
