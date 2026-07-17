# Resilience Hardening — 3 Adversarial Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate three silent failure modes identified in the adversarial stress test: turbo mode dark drift, silent OOM job drops, and AI-driven hash corruption.

**Architecture:** Each fix is surgical — one file or two. No new abstractions. Fixes are ordered by blast radius: biggest risk first. TDD throughout: write a failing test, implement the fix, confirm green, commit.

**Tech Stack:** Python 3.11+, pytest + pytest-asyncio, aiosqlite, fakeredis, pydantic v2, asyncio.

---

## Task 1: Identity Hash Split (Failure Mode 3 — AI Hash Drift)

Highest priority because it corrupts the **persistent database state** and affects every run thereafter.
The root cause: `content_hash` is computed from AI-enriched fields. When the AI model changes, all hashes change, triggering a false-discovery storm.
Fix: introduce `identity_hash` computed from **raw pre-AI fields** (url + raw title + deadline). Use `identity_hash` — not `content_hash` — for change detection.

**Files:**
- Modify: `src/domain/models.py`
- Modify: `src/application/pipeline.py`
- Modify: `src/application/post_processor.py`
- Modify: `src/infrastructure/storage/sqlite_tracker.py`
- Create: `tests/test_resilience_identity_hash.py`

---

### Step 1: Write the failing tests

Create `tests/test_resilience_identity_hash.py`:

```python
import pytest
import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch
from src.domain.models import Opportunity, RawScrapePayload
from src.application.pipeline import DataPipeline


# --- Model tests ---

def test_opportunity_has_identity_hash_field():
    """Opportunity must expose an identity_hash field."""
    t = Opportunity(
        source="test", title="Launch Services", url="https://esa.int/t1",
        source_url="https://esa.int/list"
    )
    assert hasattr(t, "identity_hash")


def test_identity_hash_is_none_by_default():
    """identity_hash starts as None before pipeline sets it."""
    t = Opportunity(
        source="test", title="Launch Services", url="https://esa.int/t1",
        source_url="https://esa.int/list"
    )
    assert t.identity_hash is None


# --- Pipeline tests ---

def test_compute_identity_hash_uses_raw_fields():
    """Identity hash must derive from url + raw title + raw deadline only."""
    pipeline = DataPipeline(ai_enrichment_enabled=False)
    t = Opportunity(
        source="esa", title="Launch Services", url="https://esa.int/t1",
        source_url="https://esa.int/list", deadline="2026-06-01"
    )
    pipeline._compute_identity_hash(t)
    expected = hashlib.md5("https://esa.int/t1|Launch Services|2026-06-01".encode()).hexdigest()
    assert t.identity_hash == expected


def test_identity_hash_stable_when_ai_changes_title():
    """
    Simulates an AI model update that rewrites the title.
    Identity hash must be identical before and after AI mutation.
    """
    pipeline = DataPipeline(ai_enrichment_enabled=False)
    t = Opportunity(
        source="esa", title="Launch Services", url="https://esa.int/t1",
        source_url="https://esa.int/list", deadline="2026-06-01"
    )
    # Compute identity hash on raw title
    pipeline._compute_identity_hash(t)
    hash_before = t.identity_hash

    # Simulate AI model rewriting the title
    t.title = "Space Launch Services Procurement - EU"
    t.summary = "Updated summary from new model"

    # Recomputing identity hash AFTER AI change must give a different result
    # because _compute_identity_hash would now use the mutated title.
    # So the contract is: compute identity hash BEFORE calling _enrich_opportunity.
    # This test verifies the pre-AI snapshot stays stable.
    assert hash_before == hashlib.md5("https://esa.int/t1|Launch Services|2026-06-01".encode()).hexdigest()


def test_identity_hash_changes_on_real_update():
    """If raw title genuinely changes (real update), identity hash must change."""
    pipeline = DataPipeline(ai_enrichment_enabled=False)
    t1 = Opportunity(
        source="esa", title="Launch Services", url="https://esa.int/t1",
        source_url="https://esa.int/list", deadline="2026-06-01"
    )
    t2 = Opportunity(
        source="esa", title="Launch Services AMENDED", url="https://esa.int/t1",
        source_url="https://esa.int/list", deadline="2026-06-01"
    )
    pipeline._compute_identity_hash(t1)
    pipeline._compute_identity_hash(t2)
    assert t1.identity_hash != t2.identity_hash


# --- Post-processor change detection tests ---

@pytest.mark.asyncio
async def test_unchanged_when_identity_hash_matches():
    """
    When identity_hash matches stored record, entity must be UNCHANGED —
    even if content_hash differs (simulating AI model drift).
    """
    from src.application.post_processor import IntelligencePostProcessor

    entity = Opportunity(
        source="esa", title="Launch Services", url="https://esa.int/t1",
        source_url="https://esa.int/list",
        content_hash="new_ai_hash_after_model_update",
        identity_hash="stable_raw_hash"
    )

    stored_record = {
        "content_hash": "old_ai_hash_before_model_update",
        "identity_hash": "stable_raw_hash",  # same → UNCHANGED
        "first_seen": "2026-01-01T00:00:00"
    }

    processor = IntelligencePostProcessor()
    processor.intel_tracker = MagicMock()
    processor.intel_tracker.get_opportunity_by_id = AsyncMock(return_value=stored_record)
    processor.intel_tracker.upsert_opportunity = AsyncMock()

    with patch("src.application.post_processor.opportunity_classifier") as mock_clf:
        mock_clf.classify.return_value = "Space"
        counts, audited = await processor.run_state_audit([entity])

    assert counts["UNCHANGED"] == 1
    assert counts["UPDATED"] == 0


@pytest.mark.asyncio
async def test_updated_when_identity_hash_changes():
    """When identity_hash differs from stored, entity must be UPDATED."""
    from src.application.post_processor import IntelligencePostProcessor

    entity = Opportunity(
        source="esa", title="Launch Services v2", url="https://esa.int/t1",
        source_url="https://esa.int/list",
        content_hash="some_hash",
        identity_hash="new_raw_hash"  # changed
    )

    stored_record = {
        "content_hash": "some_hash",
        "identity_hash": "old_raw_hash",  # different → UPDATED
        "first_seen": "2026-01-01T00:00:00"
    }

    processor = IntelligencePostProcessor()
    processor.intel_tracker = MagicMock()
    processor.intel_tracker.get_opportunity_by_id = AsyncMock(return_value=stored_record)
    processor.intel_tracker.upsert_opportunity = AsyncMock()

    with patch("src.application.post_processor.opportunity_classifier") as mock_clf:
        mock_clf.classify.return_value = "Space"
        counts, audited = await processor.run_state_audit([entity])

    assert counts["UPDATED"] == 1
    assert counts["UNCHANGED"] == 0
```

---

### Step 2: Run tests to verify they fail

```bash
cd /e/Documents/Vibe-Coding/Scraper && python -m pytest tests/test_resilience_identity_hash.py -v 2>&1 | head -40
```

Expected: `FAILED` — `Opportunity` has no `identity_hash` field, `DataPipeline` has no `_compute_identity_hash`.

---

### Step 3: Add `identity_hash` field to `Opportunity` model

In `src/domain/models.py`, add one line to the `Opportunity` class after `content_hash`:

```python
# Metadata & Tracking
content_hash: Optional[str] = Field(None, description="Hash for state tracking.")
identity_hash: Optional[str] = Field(None, description="Stable hash from raw pre-AI fields for change detection.")
```

---

### Step 4: Add `_compute_identity_hash` to `DataPipeline` and call it before enrichment

In `src/application/pipeline.py`:

**4a.** Add the new method after `_compute_content_hash`:

```python
def _compute_identity_hash(self, entity: Opportunity):
    """
    Stable identity hash computed from raw, pre-AI fields only.
    Never changes due to AI model updates — only changes on genuine data edits.
    """
    sig = f"{entity.url}|{entity.title}|{entity.deadline}"
    entity.identity_hash = hashlib.md5(sig.encode()).hexdigest()
```

**4b.** In the `process()` method, in the entity lifecycle loop, call `_compute_identity_hash` **before** `_enrich_opportunity`. Change:

```python
# BEFORE (original):
if isinstance(entity, Opportunity):
    entity.source = payload.target_site
    await self._enrich_opportunity(entity)
    self._compute_content_hash(entity)
    self._audit_integrity(entity)
    opportunities.append(entity)
```

To:

```python
# AFTER (fixed):
if isinstance(entity, Opportunity):
    entity.source = payload.target_site
    self._compute_identity_hash(entity)   # Raw fields — must be before AI enrichment
    await self._enrich_opportunity(entity)     # AI may now modify entity.title etc.
    self._compute_content_hash(entity)
    self._audit_integrity(entity)
    opportunities.append(entity)
```

---

### Step 5: Update `post_processor.py` to use `identity_hash` for change detection

In `src/application/post_processor.py`, change the hash comparison in `run_state_audit`:

```python
# BEFORE:
elif prev_state['content_hash'] != entity.content_hash:
    entity.change_type = "UPDATED"
```

```python
# AFTER:
elif (
    prev_state.get('identity_hash') and entity.identity_hash
    and prev_state['identity_hash'] != entity.identity_hash
):
    entity.change_type = "UPDATED"
elif not prev_state.get('identity_hash'):
    # Legacy record without identity_hash — fall back to content_hash comparison
    if prev_state.get('content_hash') != entity.content_hash:
        entity.change_type = "UPDATED"
    else:
        entity.change_type = "UNCHANGED"
```

---

### Step 6: Update `sqlite_tracker.py` to store and retrieve `identity_hash`

**6a.** In `initialize()`, after the existing `CREATE TABLE IF NOT EXISTS opportunities` block, add a migration-safe column addition:

```python
# After CREATE TABLE opportunities ... await db.commit():
# Migration: add identity_hash column if not present (safe to run multiple times)
try:
    await db.execute("ALTER TABLE opportunities ADD COLUMN identity_hash TEXT")
    await db.commit()
except Exception:
    pass  # Column already exists
```

**6b.** In `upsert_opportunity()`, add `identity_hash` to the INSERT columns and ON CONFLICT UPDATE:

```python
# In the INSERT VALUES tuple — add identity_hash as second-to-last before duplicate_group_id:
await db.execute("""
    INSERT INTO opportunities (
        id, source, external_id, title, buyer, country,
        publication_date, deadline, estimated_budget, currency,
        status, url, summary, normalized_budget_eur, embedding, content_hash,
        identity_hash,
        first_seen, last_seen, classification, duplicate_group_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        status = excluded.status,
        deadline = excluded.deadline,
        estimated_budget = excluded.estimated_budget,
        summary = excluded.summary,
        normalized_budget_eur = excluded.normalized_budget_eur,
        embedding = excluded.embedding,
        content_hash = excluded.content_hash,
        identity_hash = excluded.identity_hash,
        last_seen = excluded.last_seen,
        classification = excluded.classification,
        duplicate_group_id = excluded.duplicate_group_id
""", (
    opportunity_id, opportunity.source, opportunity.external_id, opportunity.title,
    opportunity.buyer, opportunity.country, opportunity.publication_date,
    opportunity.deadline, opportunity.estimated_budget, opportunity.currency,
    opportunity.status, opportunity.url, opportunity.summary, opportunity.normalized_budget_eur,
    embedding_json, opportunity.content_hash,
    opportunity.identity_hash,
    opportunity.first_seen.isoformat(), opportunity.last_seen.isoformat(),
    opportunity.classification, opportunity.duplicate_group_id
))
```

Also apply the same column addition to `upsert_opportunities_batch()`.

---

### Step 7: Run tests — all must pass

```bash
python -m pytest tests/test_resilience_identity_hash.py -v
```

Expected: all 7 tests **PASSED**.

---

### Step 8: Commit

```bash
git add src/domain/models.py src/application/pipeline.py src/application/post_processor.py src/infrastructure/storage/sqlite_tracker.py tests/test_resilience_identity_hash.py
git commit -m "fix: split identity_hash from content_hash to prevent AI model drift corrupting change detection"
```

---

## Task 2: Turbo Registry Dead Man's Switch (Failure Mode 1 — Dark Drift)

When a promoted domain starts returning empty JSON payloads (API changed), the system currently reports success forever and emits zero intelligence silently. Fix: track consecutive zero-yield turbo responses per domain; demote after 3 misses.

**Files:**
- Modify: `worker_scraper.py`
- Modify: `src/infrastructure/monitoring/observability.py`
- Create: `tests/test_resilience_turbo_guard.py`

---

### Step 1: Write the failing tests

Create `tests/test_resilience_turbo_guard.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.domain.models import ScrapeJob, RawScrapePayload
from worker_scraper import ScraperWorkerService


def make_job(url="https://api.example.com/opportunities", domain="api.example.com"):
    return ScrapeJob(
        job_id="test-job-1",
        url=url,
        target_site="test_source"
    )


def make_turbo_payload(job, json_payloads=None):
    return RawScrapePayload(
        job_id=job.job_id,
        target_site=job.target_site,
        url=job.url,
        status_code=200,
        json_payloads=json_payloads or []
    )


@pytest.mark.asyncio
async def test_turbo_miss_counter_increments_on_empty_payload():
    """Empty JSON payload from turbo scrape must increment the miss counter."""
    service = ScraperWorkerService()
    domain = "api.example.com"
    service.hybrid_registry["https://api.example.com/opportunities"] = True
    service.hybrid_domains.add(domain)

    job = make_job()
    empty_payload = make_turbo_payload(job, json_payloads=[])

    with patch.object(service, "_perform_turbo_scrape", return_value=empty_payload), \
         patch.object(service.queue, "push_raw_payload", new_callable=AsyncMock), \
         patch("worker_scraper.metrics_tracker") as mock_metrics:
        mock_metrics.record_job_status = AsyncMock()
        mock_metrics.increment = AsyncMock()
        await service.process_job(job)

    assert service._turbo_miss_counts.get(domain, 0) == 1


@pytest.mark.asyncio
async def test_turbo_domain_demoted_after_threshold_misses():
    """After TURBO_MISS_THRESHOLD consecutive empty yields, domain must be evicted."""
    service = ScraperWorkerService()
    domain = "api.example.com"
    url = "https://api.example.com/opportunities"
    service.hybrid_registry[url] = True
    service.hybrid_domains.add(domain)
    service._turbo_miss_counts[domain] = service.TURBO_MISS_THRESHOLD - 1

    job = make_job(url=url)
    empty_payload = make_turbo_payload(job, json_payloads=[])

    with patch.object(service, "_perform_turbo_scrape", return_value=empty_payload), \
         patch.object(service.queue, "push_raw_payload", new_callable=AsyncMock), \
         patch("worker_scraper.metrics_tracker") as mock_metrics:
        mock_metrics.record_job_status = AsyncMock()
        mock_metrics.increment = AsyncMock()
        await service.process_job(job)

    assert url not in service.hybrid_registry
    assert domain not in service.hybrid_domains
    assert domain not in service._turbo_miss_counts
    mock_metrics.increment.assert_any_call("turbo_yield_failure")


@pytest.mark.asyncio
async def test_turbo_miss_counter_resets_on_successful_yield():
    """Non-empty JSON payload must reset the miss counter for that domain."""
    service = ScraperWorkerService()
    domain = "api.example.com"
    url = "https://api.example.com/opportunities"
    service.hybrid_registry[url] = True
    service.hybrid_domains.add(domain)
    service._turbo_miss_counts[domain] = 2  # pre-populated misses

    job = make_job(url=url)
    good_payload = make_turbo_payload(job, json_payloads=[{"url": url, "data": {"results": [1, 2]}}])

    with patch.object(service, "_perform_turbo_scrape", return_value=good_payload), \
         patch.object(service.queue, "push_raw_payload", new_callable=AsyncMock), \
         patch("worker_scraper.metrics_tracker") as mock_metrics:
        mock_metrics.record_job_status = AsyncMock()
        mock_metrics.increment = AsyncMock()
        await service.process_job(job)

    assert domain not in service._turbo_miss_counts
    assert url in service.hybrid_registry  # still promoted


def test_turbo_yield_failure_in_metric_keys():
    """turbo_yield_failure must be a tracked metric key."""
    from src.infrastructure.monitoring.observability import ObservabilityMetrics
    m = ObservabilityMetrics()
    assert "turbo_yield_failure" in m.metric_keys
```

---

### Step 2: Run tests to verify they fail

```bash
python -m pytest tests/test_resilience_turbo_guard.py -v 2>&1 | head -30
```

Expected: `FAILED` — `ScraperWorkerService` has no `_turbo_miss_counts` or `TURBO_MISS_THRESHOLD`.

---

### Step 3: Add `turbo_yield_failure` to observability metric keys

In `src/infrastructure/monitoring/observability.py`, add to `self.metric_keys` list:

```python
self.metric_keys = [
    "jobs_total", "jobs_success", "jobs_failed",
    "captcha_encountered", "proxy_failures",
    "pages_scraped", "llm_fallbacks_triggered",
    "turbo_yield_failure",   # NEW: domains demoted due to empty turbo responses
    "jobs_dropped_oom",      # NEW: jobs silently dropped under OOM (added in Task 3)
    "fanout_cap_drops",      # NEW: recursive jobs dropped at fan-out cap (added in Task 4)
]
```

(Adding all three new metric keys now to avoid touching this file again.)

---

### Step 4: Add turbo miss tracking to `ScraperWorkerService`

In `worker_scraper.py`:

**4a.** Add class constant and instance variable in `__init__`:

```python
class ScraperWorkerService:
    TURBO_MISS_THRESHOLD = 3  # consecutive empty yields before domain demotion

    def __init__(self):
        self.queue = RedisQueueWorker()
        self.context_pool = BrowserContextPool(pool_size=2)
        self.hybrid_registry = {}
        self.hybrid_domains = set()
        self._turbo_miss_counts: dict = {}  # domain -> consecutive empty yield count
```

**4b.** In `process_job()`, replace the turbo mode success block:

```python
# BEFORE (original):
try:
    raw_payload = await self._perform_turbo_scrape(job)
    await metrics_tracker.record_job_status(success=True)
    await self.queue.push_raw_payload("raw_data_queue", raw_payload)
    return
except Exception as e:
    logger.warning(f"Spacescraper Turbo Fault: Falling back to Browser context. Error: {e}")
```

```python
# AFTER (fixed):
try:
    raw_payload = await self._perform_turbo_scrape(job)

    if not raw_payload.json_payloads:
        # Semantic failure: transport succeeded but no data returned
        miss_count = self._turbo_miss_counts.get(domain, 0) + 1
        self._turbo_miss_counts[domain] = miss_count
        if miss_count >= self.TURBO_MISS_THRESHOLD:
            logger.warning(
                f"Spacescraper: Turbo yield failure for {domain} "
                f"({miss_count} consecutive empty responses). Demoting to browser mode."
            )
            self.hybrid_registry.pop(job.url, None)
            self.hybrid_domains.discard(domain)
            self._turbo_miss_counts.pop(domain, None)
            await metrics_tracker.increment("turbo_yield_failure")
    else:
        # Successful yield — reset miss counter
        self._turbo_miss_counts.pop(domain, None)

    await metrics_tracker.record_job_status(success=True)
    await self.queue.push_raw_payload("raw_data_queue", raw_payload)
    return
except Exception as e:
    logger.warning(f"Spacescraper Turbo Fault: Falling back to Browser context. Error: {e}")
```

---

### Step 5: Run tests — all must pass

```bash
python -m pytest tests/test_resilience_turbo_guard.py -v
```

Expected: all 4 tests **PASSED**.

---

### Step 6: Commit

```bash
git add worker_scraper.py src/infrastructure/monitoring/observability.py tests/test_resilience_turbo_guard.py
git commit -m "fix: add turbo registry dead man's switch to auto-demote domains on consecutive empty yields"
```

---

## Task 3: OOM Job Drop — DLQ + Metric (Failure Mode 2, Part A)

When Redis hits the hard memory limit, jobs are currently **silently discarded** — no DLQ entry, no metric. Fix: route dropped jobs to DLQ and increment a metric counter so the loss is visible and recoverable.

**Files:**
- Modify: `src/infrastructure/queues/redis_worker.py`
- Create: `tests/test_resilience_oom_dlq.py`

---

### Step 1: Write the failing tests

Create `tests/test_resilience_oom_dlq.py`:

```python
import pytest
import json
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call
from src.domain.models import ScrapeJob
from src.infrastructure.queues.redis_worker import RedisQueueWorker


def make_job(job_id="job-oom-1"):
    return ScrapeJob(job_id=job_id, url="https://example.com", target_site="test")


@pytest.mark.asyncio
async def test_oom_drop_pushes_to_dlq():
    """When OOM hard limit is hit, job must be pushed to DLQ, not silently dropped."""
    worker = RedisQueueWorker()
    worker._is_mock = False

    job = make_job()
    dlq_pushes = []

    async def mock_info(section=None):
        # Simulate Redis memory above hard limit (1.5x soft = 768MB)
        return {"used_memory_rss": 800 * 1024 * 1024}  # 800MB

    async def mock_rpush(queue_name, payload):
        dlq_pushes.append((queue_name, payload))

    worker.redis = MagicMock()
    worker.redis.info = mock_info
    worker.redis.rpush = mock_rpush
    worker.redis.incrby = AsyncMock()

    await worker.push_job("jobs_queue", job)

    # Verify DLQ received the job
    assert any("dlq" in name for name, _ in dlq_pushes), \
        "Expected DLQ push but none found"


@pytest.mark.asyncio
async def test_oom_drop_increments_metric():
    """When OOM hard limit is hit, jobs_dropped_oom metric must be incremented."""
    worker = RedisQueueWorker()
    worker._is_mock = False

    job = make_job()
    incr_calls = []

    async def mock_info(section=None):
        return {"used_memory_rss": 800 * 1024 * 1024}

    async def mock_rpush(queue_name, payload):
        pass

    async def mock_incrby(key, amount):
        incr_calls.append((key, amount))

    worker.redis = MagicMock()
    worker.redis.info = mock_info
    worker.redis.rpush = mock_rpush
    worker.redis.incrby = mock_incrby

    await worker.push_job("jobs_queue", job)

    assert any("dropped_oom" in key for key, _ in incr_calls), \
        f"Expected jobs_dropped_oom increment but got: {incr_calls}"


@pytest.mark.asyncio
async def test_normal_job_not_affected_below_threshold():
    """Jobs below the soft memory limit must be enqueued normally."""
    worker = RedisQueueWorker()
    worker._is_mock = False

    job = make_job()
    pushed = []

    async def mock_info(section=None):
        return {"used_memory_rss": 100 * 1024 * 1024}  # 100MB — well below limit

    async def mock_rpush(queue_name, payload):
        pushed.append(queue_name)

    worker.redis = MagicMock()
    worker.redis.info = mock_info
    worker.redis.rpush = mock_rpush

    await worker.push_job("jobs_queue", job)

    assert "jobs_queue" in pushed
    assert not any("dlq" in q for q in pushed)
```

---

### Step 2: Run tests to verify they fail

```bash
python -m pytest tests/test_resilience_oom_dlq.py -v 2>&1 | head -30
```

Expected: `FAILED` — OOM branch does `return` without DLQ push.

---

### Step 3: Fix the OOM drop in `redis_worker.py`

In `src/infrastructure/queues/redis_worker.py`, replace the hard-limit block in `push_job()`:

```python
# BEFORE (original):
if used_memory > (self.memory_limit_mb * 1.5): # Hard limit
    logger.error(f"Spacescraper CRITICAL: Cluster Saturation. Dropping job {job.job_id} to prevent OOM crash.")
    return
```

```python
# AFTER (fixed):
if used_memory > (self.memory_limit_mb * 1.5):  # Hard limit
    logger.error(
        f"Spacescraper CRITICAL: Cluster Saturation. Routing job {job.job_id} to DLQ to prevent OOM crash."
    )
    # Route to DLQ so the loss is visible and recoverable (not silently discarded)
    dlq_name = f"{queue_name}_dlq"
    entry = json.dumps({"error": "OOM_BACKPRESSURE", "data": job.model_dump_json()})
    await self.redis.rpush(dlq_name, entry)
    # Increment metric directly via redis to avoid circular import
    await self.redis.incrby("metrics:jobs_dropped_oom", 1)
    return
```

---

### Step 4: Run tests — all must pass

```bash
python -m pytest tests/test_resilience_oom_dlq.py -v
```

Expected: all 3 tests **PASSED**.

---

### Step 5: Commit

```bash
git add src/infrastructure/queues/redis_worker.py tests/test_resilience_oom_dlq.py
git commit -m "fix: route OOM-dropped jobs to DLQ and increment jobs_dropped_oom metric instead of silent discard"
```

---

## Task 4: Recursive Fan-Out Cap (Failure Mode 2, Part B)

A portal mass-update can generate thousands of recursive jobs, flooding Redis and triggering OOM. Fix: cap the total number of recursive children per root job at 200, tracked in Redis with a 1-hour TTL. Overflow jobs are dropped to DLQ (now safe from Task 3).

**Files:**
- Modify: `src/infrastructure/queues/redis_worker.py` (add fan-out helper)
- Modify: `worker_processor.py` (apply cap before queuing follow-urls)
- Create: `tests/test_resilience_fanout_cap.py`

---

### Step 1: Write the failing tests

Create `tests/test_resilience_fanout_cap.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.domain.models import RawScrapePayload, ProcessingResult, FollowLink, Opportunity
from worker_processor import ProcessorWorkerService


def make_payload(job_id="root-job-1", depth=0):
    return RawScrapePayload(
        job_id=job_id,
        target_site="test_source",
        url="https://example.com/listing",
        status_code=200,
        html_content="<html></html>"
    )


def make_follow_links(count, base_url="https://example.com/opportunity/"):
    return [{"url": f"{base_url}{i}", "target_site": "test_source", "depth": 1}
            for i in range(count)]


@pytest.mark.asyncio
async def test_follow_urls_within_cap_all_enqueued():
    """Follow URLs within the MAX_RECURSIVE_FANOUT limit must all be queued."""
    service = ProcessorWorkerService()
    payload = make_payload()

    result = ProcessingResult(
        job_id=payload.job_id,
        success=True,
        entities=[],
        follow_urls=make_follow_links(10)
    )

    enqueued = []

    async def mock_push_job(queue_name, job):
        enqueued.append(job.url)

    async def mock_fanout_check(root_id, count, max_fanout):
        return count  # All allowed

    with patch.object(service.pipeline, "process", return_value=result), \
         patch.object(service.post_processor, "run_state_audit",
                      return_value=({"NEW": 0, "UPDATED": 0, "UNCHANGED": 0}, [])), \
         patch.object(service.queue, "push_job", side_effect=mock_push_job), \
         patch.object(service.queue, "get_allowed_fanout",
                      side_effect=mock_fanout_check), \
         patch("worker_processor.metrics_tracker") as mock_metrics:
        mock_metrics.increment = AsyncMock()
        mock_metrics.record_job_status = AsyncMock()
        await service.process_payload(payload)

    assert len(enqueued) == 10


@pytest.mark.asyncio
async def test_follow_urls_over_cap_are_limited():
    """Follow URLs exceeding MAX_RECURSIVE_FANOUT must be capped; excess dropped to DLQ."""
    service = ProcessorWorkerService()
    payload = make_payload()

    result = ProcessingResult(
        job_id=payload.job_id,
        success=True,
        entities=[],
        follow_urls=make_follow_links(250)  # Exceeds cap of 200
    )

    enqueued = []
    dlq_pushes = []

    async def mock_push_job(queue_name, job):
        enqueued.append(job.url)

    async def mock_push_dlq(queue_name, job, reason):
        dlq_pushes.append((job.url, reason))

    async def mock_fanout_check(root_id, count, max_fanout):
        return min(count, max_fanout)  # Cap at max

    with patch.object(service.pipeline, "process", return_value=result), \
         patch.object(service.post_processor, "run_state_audit",
                      return_value=({"NEW": 0, "UPDATED": 0, "UNCHANGED": 0}, [])), \
         patch.object(service.queue, "push_job", side_effect=mock_push_job), \
         patch.object(service.queue, "push_dead_letter", side_effect=mock_push_dlq), \
         patch.object(service.queue, "get_allowed_fanout",
                      side_effect=mock_fanout_check), \
         patch("worker_processor.metrics_tracker") as mock_metrics:
        mock_metrics.increment = AsyncMock()
        mock_metrics.record_job_status = AsyncMock()
        await service.process_payload(payload)

    assert len(enqueued) == service.MAX_RECURSIVE_FANOUT
    assert len(dlq_pushes) == 250 - service.MAX_RECURSIVE_FANOUT
    mock_metrics.increment.assert_any_call("fanout_cap_drops")
```

---

### Step 2: Run tests to verify they fail

```bash
python -m pytest tests/test_resilience_fanout_cap.py -v 2>&1 | head -30
```

Expected: `FAILED` — `ProcessorWorkerService` has no `MAX_RECURSIVE_FANOUT`, `RedisQueueWorker` has no `get_allowed_fanout`.

---

### Step 3: Add `get_allowed_fanout` to `RedisQueueWorker`

In `src/infrastructure/queues/redis_worker.py`, add after `push_dead_letter`:

```python
async def get_allowed_fanout(self, root_job_id: str, requested: int, max_fanout: int) -> int:
    """
    Atomic fan-out budget check. Returns how many of the `requested` child jobs
    are actually allowed under the per-root cap. Excess is 0.
    Uses Redis INCR with a 1-hour TTL so the budget resets naturally.
    """
    if not self.redis or self._is_mock:
        return requested  # No cap in mock/dev mode

    fanout_key = f"fanout:{root_job_id}"
    try:
        current = int(await self.redis.get(fanout_key) or 0)
        available = max(0, max_fanout - current)
        allowed = min(requested, available)
        if allowed > 0:
            await self.redis.incrby(fanout_key, allowed)
            await self.redis.expire(fanout_key, 3600)  # 1-hour TTL
        return allowed
    except Exception as e:
        logger.warning(f"Fan-out check failed ({e}), allowing all jobs.")
        return requested  # Fail open to avoid blocking legitimate jobs
```

---

### Step 4: Add fan-out cap logic to `ProcessorWorkerService`

In `worker_processor.py`:

**4a.** Add class constant:

```python
class ProcessorWorkerService:
    MAX_RECURSIVE_FANOUT = 200  # max child jobs per root job to prevent OOM floods
```

**4b.** Replace the follow-url queuing loop in `process_payload()`:

```python
# BEFORE (original):
for follow in result.follow_urls:
    new_job = ScrapeJob(
        job_id=f"rec_{payload.job_id}",
        ...
    )
    await self.queue.push_job("jobs_queue", new_job)
```

```python
# AFTER (fixed):
if result.follow_urls:
    root_id = payload.job_id.split("rec_", 1)[-1] if "rec_" in payload.job_id else payload.job_id
    allowed_count = await self.queue.get_allowed_fanout(
        root_id, len(result.follow_urls), self.MAX_RECURSIVE_FANOUT
    )
    dropped_count = len(result.follow_urls) - allowed_count

    for follow in result.follow_urls[:allowed_count]:
        new_job = ScrapeJob(
            job_id=f"rec_{payload.job_id}",
            url=follow['url'],
            target_site=follow['target_site'],
            depth=follow.get('depth', 0),
            persona_id=payload.persona_id if hasattr(payload, 'persona_id') else None,
            overlay=payload.overlay if hasattr(payload, 'overlay') else None,
            webhook_url=payload.webhook_url if hasattr(payload, 'webhook_url') else None,
        )
        await self.queue.push_job("jobs_queue", new_job)

    if dropped_count > 0:
        logger.warning(
            f"Spacescraper: Fan-out cap hit for root {root_id}. "
            f"Allowed {allowed_count}/{len(result.follow_urls)} recursive jobs. "
            f"Dropping {dropped_count} to DLQ."
        )
        for follow in result.follow_urls[allowed_count:]:
            overflow_job = ScrapeJob(
                job_id=f"rec_{payload.job_id}",
                url=follow['url'],
                target_site=follow['target_site'],
                depth=follow.get('depth', 0),
            )
            await self.queue.push_dead_letter("jobs_queue", overflow_job, reason="FANOUT_CAP_EXCEEDED")
        await metrics_tracker.increment("fanout_cap_drops")
```

---

### Step 5: Run all resilience tests together

```bash
python -m pytest tests/test_resilience_identity_hash.py tests/test_resilience_turbo_guard.py tests/test_resilience_oom_dlq.py tests/test_resilience_fanout_cap.py -v
```

Expected: all tests **PASSED**.

---

### Step 6: Run full test suite to check for regressions

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: all pre-existing tests still pass.

---

### Step 7: Commit

```bash
git add worker_processor.py src/infrastructure/queues/redis_worker.py tests/test_resilience_fanout_cap.py
git commit -m "fix: add recursive fan-out cap (200/root-job) with DLQ overflow to prevent Redis OOM floods"
```

---

## Final Verification

Run the full test suite one more time:

```bash
python -m pytest tests/ -v 2>&1 | tail -20
```

Then confirm the three metrics are visible in the observability layer:

```bash
python -c "
from src.infrastructure.monitoring.observability import ObservabilityMetrics
m = ObservabilityMetrics()
required = {'turbo_yield_failure', 'jobs_dropped_oom', 'fanout_cap_drops'}
missing = required - set(m.metric_keys)
print('PASS: all resilience metrics registered' if not missing else f'FAIL: missing {missing}')
"
```

Expected output: `PASS: all resilience metrics registered`
