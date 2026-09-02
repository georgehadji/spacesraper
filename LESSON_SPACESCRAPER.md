# Spacescraper: From Procurement Scraper to Generic Extraction Platform

## What We Built

Spacescraper started as a specialized procurement intelligence scraper — extracting defense contracts from ESA, NATO, and government portals — and evolved into a **generic, schema-driven web extraction platform**. The transformation touched every layer: security, architecture, data models, queue infrastructure, and extraction strategy.

The system now handles **any** web page as a target. It dispatches extraction jobs through a FastAPI gateway, processes them through a 3-worker pipeline (scraper → processor → reporter) connected by Valkey Streams, persists everything in SQLite with a PostgreSQL migration path, and continuously learns which extraction strategies work best per domain through strategy observation and shadow evaluation.

**Scale:** ~57 Python modules, 72 passing tests, 5 workers, 5 repository adapters, 6 extraction strategies.

---

## Why Architecture Matters: The Hexagonal Pivot

The original codebase was a tangled set of singletons. `main.py` created module-level `RedisQueueWorker()`, `api_key_manager`, and `ai_orchestrator` instances. Worker files held their own duplicate compositions. Application code imported from infrastructure directly — `post_processor.py` imported `SqliteTracker`, `pipeline.py` imported `ai_orchestrator` at module scope.

This meant:
- **Tests couldn't inject mocks.** Every test had to patch module-level singletons.
- **Changing a dependency required editing every consumer.**
- **The dependency graph was invisible.** You couldn't see what depended on what without tracing every import.

### The Fix: Ports and Adapters

We introduced `src/domain/ports.py` — a file containing only `Protocol` classes. Each protocol defines a contract:

```python
class JobRepository(Protocol):
    async def create_job(self, job: Job) -> Job: ...
    async def get_job(self, job_id: str) -> Optional[Job]: ...
    async def update_job_state(self, job_id: str, state: JobState, ...) -> Optional[Job]: ...
```

The infrastructure layer implements these contracts:

```python
class SqliteJobRepository:
    async def create_job(self, job: Job) -> Job:
        assert self._conn is not None
        await self._conn.execute("INSERT INTO jobs (...) VALUES (...)", ...)
```

Application code depends on the protocol, not the implementation:

```python
class StrategyEvaluator:
    def __init__(self, obs_repo: ObservationRepository):
        self.obs_repo = obs_repo  # accepts ANY ObservationRepository
```

A single composition root (`src/bootstrap.py`) wires everything:

```python
job_repo = SqliteJobRepository()
outbox_repo = SqliteOutboxRepository()
evaluator = StrategyEvaluator(obs_repo=obs_repo)
```

**Why this matters for your projects:** Every non-trivial application should have a ports/contracts file and a composition root. The ports file is your architecture documentation — it tells you what the application *does* without showing *how*. The composition root is your dependency map — one file that shows every concrete adapter and who gets it.

**Trade-off:** More files, more indirection. For a 50-line script this is overkill. For anything with multiple entry points, async workers, or database adapters, it prevents the "import spaghetti" that kills maintainability.

---

## Security Is Not a Feature

Three security issues were fixed in Phase 0. Each illustrates a different class of vulnerability.

### 1. CORS: Wildcard + Credentials

The original code had:

```python
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True)
```

This combination is **blocked by all modern browsers**. `allow_credentials=True` with a wildcard origin is explicitly forbidden by the CORS spec (it would let any website make authenticated requests). The fix uses an explicit origin list from environment configuration:

```python
app.add_middleware(CORSMiddleware, allow_origins=build_cors_origins(), allow_credentials=True)
```

`build_cors_origins()` reads `CORS_ALLOWED_ORIGINS` from `.env` and returns a list — never a wildcard.

### 2. Authentication: The `ss_` Bypass

Any API key starting with `ss_` was accepted without validation:

```python
if key.startswith("ss_"):
    return  # <-- accepted all keys with this prefix
```

An attacker only needed to know the prefix `ss_` to access every endpoint. The fix stores key hashes in memory and validates against stored keys only:

```python
async def validate_key(key: str) -> bool:
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    return key_hash in self._keys_by_hash
```

### 3. SSRF: URLs as Attack Surface

The `/jobs` endpoint accepted a user-supplied URL. Without validation, an attacker could:
- Probe internal services: `http://169.254.169.254/latest/meta-data/` (AWS metadata)
- Scan internal networks: `http://10.0.0.1/admin`
- Trigger outbound connections to arbitrary hosts

The fix adds an SSRF guard with DNS-resolving validation:

```python
def validate_outbound_url(url: str, require_https: bool = False) -> str:
    hostname = urlparse(url).hostname
    ip = socket.gethostbyname(hostname)
    if ipaddress.ip_address(ip).is_private:
        raise SSRFGuardError(f"Blocked internal IP: {ip}")
```

Same guard applies to webhook URLs with `require_https=True` in production.

**Lesson for your projects:** Security isn't something you add later. CORS misconfiguration, broken auth, and SSRF are the three most common vulnerabilities in web scraping/API projects. All three were present in a single file (`main.py`). Audit these three things first.

---

## The Job Lifecycle: From Fire-and-Forget to Durable

The original job submission produced a Redis message and returned `{"status": "accepted"}` — with no way to check what happened afterward. The job was fire-and-forget.

We added a **6-state state machine** with strict transition guards:

```python
class JobState(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    DEAD_LETTERED = "DEAD_LETTERED"

    def can_transition_to(self, target: "JobState") -> bool:
        allowed = {
            JobState.QUEUED: {JobState.RUNNING, JobState.CANCELLED, JobState.DEAD_LETTERED},
            JobState.RUNNING: {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED},
            JobState.SUCCEEDED: set(),    # terminal
            JobState.FAILED: {JobState.QUEUED, JobState.DEAD_LETTERED},
            JobState.CANCELLED: set(),    # terminal
            JobState.DEAD_LETTERED: set(), # terminal
        }
        return target in allowed.get(self, set())
```

Every state change goes through `Job.transition_to()`:

```python
def transition_to(self, new_state: JobState) -> "Job":
    if not self.state.can_transition_to(new_state):
        raise ValueError(f"Invalid state transition: {self.state.value} -> {new_state.value}")
    return self.model_copy(update={"state": new_state, "updated_at": datetime.utcnow()})
```

The worker records each execution attempt in a `JobAttempt` table:

```python
class JobAttempt(BaseModel):
    attempt_id: str
    job_id: str
    state: JobState
    started_at: datetime
    finished_at: Optional[datetime]
    worker_id: Optional[str]
    error_message: Optional[str]
```

And the API exposes job status via `GET /jobs/{job_id}` and cancellation via `POST /jobs/{job_id}/cancel`.

**Lesson for your projects:** If your system processes tasks asynchronously, every task needs: (1) a defined state machine, (2) a durable status endpoint, and (3) immutable execution attempts for auditing. The cost of adding these after launch is 10x higher than building them from the start.

---

## Queues and Streams: Why Redis Lists Weren't Enough

The original system used Redis Lists (`BLPOP`/`RPUSH`) for job queuing. This was simple but had critical limitations:

| Redis Lists | Redis Streams (Valkey) |
|---|---|
| At-most-once delivery | At-least-once with consumer groups |
| No message acknowledgment | `XACK` confirms processing |
| No re-delivery on failure | `XPENDING` + `XCLAIM` recover failed messages |
| No dead-letter queue | Built-in DLQ via `push_dlq()` |
| Single consumer per list | Multiple consumers in a group |

The migration introduced a typed message envelope:

```python
class QueueMessage(BaseModel):
    message_id: str
    message_type: MessageType  # SCRAPE_JOB | RAW_PAYLOAD | DISCOVERY_EVENT | JOB_CANCEL
    correlation_id: Optional[str]
    root_job_id: Optional[str]
    payload: Dict[str, Any]
    retry_count: int = 0
    max_retries: int = 3
```

Each message has a type discriminator, a correlation ID for tracing, retry tracking, and a schema version for future migration. The stream queue adapter implements the full lifecycle:

```python
class RedisStreamQueue:
    async def push(self, stream: str, message: QueueMessage) -> str:
        """XADD a typed message to a stream."""
    
    async def consume(self, stream: str, group: str, consumer: str, callback):
        """XREADGROUP loop with ACK/NACK, retry, and DLQ."""
    
    async def _process_entry(self, entry_id, data, callback):
        """Deserialize, execute callback, ACK on success or DLQ on exhaustion."""
```

**Why this matters:** If your queue system can't tell the difference between "message processed successfully" and "consumer crashed," you'll eventually lose data. Redis Streams with consumer groups give you exactly-once processing semantics through acknowledgment and pending message recovery. The typed envelope ensures you always know what you're deserializing.

---

## The Outbox Pattern: Reliable Events Without Distributed Transactions

When a job is submitted, two things need to happen: (1) the job is persisted in SQLite, and (2) a message is published to Valkey Streams for the scraper worker. These are two different systems. A distributed transaction is a non-starter with SQLite.

The solution: **write the event to the same SQLite database first**, then relay it to the stream asynchronously.

```python
# In main.py -> submit_job:
job = Job(job_id=job_id, url=..., ...)
await job_repo.create_job(job)

# Create outbox event atomically with the job
event = OutboxEvent(
    aggregate_type="job",
    aggregate_id=job_id,
    event_type="job.submitted",
    payload={"url": str(submission.url), ...},
)
await outbox_repo.create_event(event)
```

A background relay service drains the outbox:

```python
class OutboxRelay:
    async def run_forever(self):
        while True:
            events = await self.outbox_repo.get_pending_events(limit=50)
            for event in events:
                try:
                    await self.stream_queue.push("jobs_stream", event.to_message())
                    await self.outbox_repo.mark_delivered(event.event_id)
                except Exception as e:
                    await self.outbox_repo.mark_failed(event.event_id, str(e))
```

The relay uses **token-bucket backpressure**: when the stream is full, the relay slows down. When it catches up, it speeds up. This prevents the relay from crashing when the stream consumer is temporarily unavailable.

**Lesson for your projects:** Whenever you have a local write (database) and a message publication (queue/stream/Kafka) that need to be consistent, use the outbox pattern. The event lives with the data. If the relay crashes, events queue up in SQLite and are delivered when it restarts. Zero data loss.

---

## Generic Extraction: Schemas Over Special Cases

The original system had four domain-specific entity types — `Product`, `Opportunity`, `Lead`, `Article` — each with its own extraction logic, its own deduplication code, and its own data model. Adding a new data type meant writing a new Pydantic model, a new extractor class, a new storage adapter, and a new report generator.

We replaced all four with a single generic model and a schema-based extraction pipeline.

### The Generic Record

```python
class ExtractedRecord(BaseModel):
    record_id: str
    record_type: str = "generic"       # "product", "listing", "article", etc.
    schema_version: str = "1.0"
    source_url: str
    data: Dict[str, Any]               # validated against ExtractionSchema
    identity_hash: Optional[str]       # for change detection
    content_hash: Optional[str]        # for full-content tracking
    change_type: ChangeType            # NEW | UPDATED | UNCHANGED
```

### Schema-Driven Validation

```python
class ExtractionSchema(BaseModel):
    schema_id: str
    fields: List[FieldDefinition]
    quality_rules: Dict[str, Any]

    def validate_record(self, data: Dict[str, Any]) -> List[str]:
        """Validate against schema fields. Returns list of errors."""
```

A `FieldDefinition` specifies name, type (`string` / `number` / `boolean` / `url`), whether it's required, and whether it contributes to the `identity_hash`.

### The Strategy Chain

Extraction now follows a deterministic priority chain:

```
1. page_fields (user-specified CSS selectors)        — highest priority
2. Google Maps Place page (GoogleMapsPlaceStrategy)
3. Google Maps Search (GoogleMapsStrategy)            — domain-specific
4. ExtractionOverlay (declarative field mappings)     — declarative
5. JSON-LD structured data                            — generic fallback
6. Semantic HTML patterns (articles, tables, lists)   — last resort
```

Each stage can produce `ExtractedRecord` objects. A failed stage does not erase results from earlier stages. The `OverrideStrategy` lets users specify `field_name -> CSS selector` mappings at job submission time — bypassing all other strategies:

```python
POST /jobs
{
    "url": "https://books.toscrape.com",
    "page_fields": {
        "title": "h3 a",
        "price": ".price_color"
    },
    "container_selector": "article.product_pod"
}
```

### Google Maps Integration

The integration with the `google-maps-scraper` project is a case study in this architecture. Rather than running the Go binary as a sidecar service, we extracted its *knowledge* (DOM selectors, JSON parsing logic, grid math) and recast it as three components in Spacescraper's framework:

1. **`GoogleMapsStrategy`** — parses Google Maps internal JSON payloads (the `data` arrays embedded in search result pages) into `ExtractedRecord` objects with 16 field mappings
2. **`GoogleMapsPlaceStrategy`** — extracts richer data from individual place detail pages, including reviews and popular times
3. **`url_to_grid_cells()`** — subdivides geographic bounding boxes to bypass Maps' ~120 result limit

The Go project was ~31,000 lines. We kept ~2,000 lines of extraction knowledge and discarded the 90% that was infrastructure (runners, web UI, cloud provisioning, job queue) — because Spacescraper already had that infrastructure.

**Lesson for your projects:** Don't build data models around your first use case. Build a generic record type with schema validation from day one. When you need a new data type, add a schema — not a model class, not an extractor, not a storage adapter. Just a JSON or YAML definition of what fields matter.

---

## AI as an Adapter, Not the Engine

The original system embedded AI as a hard dependency — the `pipeline.py` computed embeddings for every opportunity, the `llm_enrichment.py` called OpenAI for enrichment, and the `_is_similar()` method used cosine similarity as a primary deduplication signal.

We moved AI to the infrastructure boundary where it belongs: an optional enrichment provider behind a protocol, with caching and circuit-breaking.

### The Protocol

```python
class EnrichmentProvider(ABC):
    @abstractmethod
    async def enrich(self, text: str, prompt: str) -> Optional[str]: ...
    @abstractmethod
    async def compute_embedding(self, text: str) -> Optional[List[float]]: ...
```

Two implementations:
- **`NoOpEnrichmentProvider`** — returns `None` for everything. Used in tests and when AI is disabled.
- **`GeminiEnrichmentProvider`** — calls Google Gemini API with circuit breaker.

### Two-Level Cache

```python
class AICache:
    def __init__(self, redis_url: str, lru_size: int = 1000):
        self._lru: OrderedDict[str, str] = OrderedDict()  # in-memory LRU
        self._valkey: Optional[valkey.Redis] = None         # distributed cache
    
    async def get_or_compute(self, key: str, computer) -> str:
        # 1. Check LRU (fast, local)
        val = self._lru.get(key)
        if val is not None:
            self._lru.move_to_end(key)
            return val
        # 2. Check Valkey (shared across workers)
        if self._valkey:
            val = await self._valkey.get(key)
            if val is not None:
                self._lru[key] = val
                return val
        # 3. Compute, store in both
        result = await computer()
        self._lru[key] = result
        if self._valkey:
            await self._valkey.setex(key, 3600, result)
        return result
```

### Circuit Breaker

```python
class AIOrchestrator:
    async def _check_circuit(self) -> bool:
        if self.failure_count >= self.breaker_threshold:
            if datetime.utcnow() < self.offline_until:
                return False  # circuit open — fail fast
            self.failure_count = 0  # cooling period expired
        return True
```

After 5 consecutive failures, the circuit opens and all requests fail fast for 300 seconds. This prevents cascading failures when the AI provider is degraded.

**Lesson for your projects:** AI is infrastructure, not domain logic. Put it behind a protocol with a NoOp implementation. Cache aggressively (two levels: LRU + distributed). Circuit-break to prevent cascading failures. Never make AI a hard dependency in your data pipeline — always have a fallback path.

---

## Learning Without Breaking Production

The system records every extraction attempt as a `StrategyObservation`:

```python
class StrategyObservation(BaseModel):
    domain: str          # which domain was scraped
    strategy: str        # "http", "browser", "overlay", "json_ld", "semantic_html"
    valid_record_count: int
    required_field_completeness: float
    duplicate_rate: float
    http_status: Optional[int]
    blocked: bool
    latency_ms: float
    cost: float
    success: bool
```

These observations feed two learning mechanisms:

### Shadow Evaluation

When a new overlay is created (state: `CANDIDATE`), it runs in parallel with the ACTIVE overlay but its results aren't used. A `ShadowOverlayEvaluator` compares candidate vs. active results:

- If the candidate extracts more valid records → scores higher
- If the candidate has lower latency or lower block rate → scores higher
- After enough observations → promotes to ACTIVE

### Exploration with Thompson Sampling

The `StrategySelector` periodically evaluates which strategy performs best per domain using Thompson sampling. It doesn't just pick the best observed strategy — it allocates some percentage of traffic to exploration (trying less-used strategies) to discover improvements.

```python
class ExplorationPolicy:
    def select(self, domain: str, stats: Dict[str, StrategyStats]) -> str:
        # Thompson sampling: sample from beta distribution of each strategy's success rate
        # The strategy with the highest sampled value wins
```

**Lesson for your projects:** Record what happens. A `StrategyObservation` costs a few bytes per extraction. After 100 observations, you know which strategy works best for which domain. After 1,000, you have statistical confidence. This is the difference between "we think X works" and "we know X produces 23% more valid records than Y."

---

## Valkey Over Redis: A Dependency Choice

The project migrated from `redis-py` to `valkey-py`. Why?

Valkey is a fork of Redis 7.2, created after Redis Ltd. changed the Redis license from BSD to SSPL (a source-available license that restricts cloud providers). Valkey maintains the BSD license and is backed by the Linux Foundation.

From the application's perspective, **nothing changed**. The API is identical:

```python
# Before (redis):
import redis.asyncio as redis
client = redis.from_url("redis://localhost:6379")

# After (valkey):
import valkey.asyncio as valkey
client = valkey.from_url("valkey://localhost:6379")
```

All Redis data structures work identically: Streams, Sorted Sets, Hashes, Lists, Pub/Sub. The migration was a find-and-replace across 5 source files.

**When does this matter?**
- **If you're self-hosting:** Valkey is the safer long-term choice — BSD license, Linux Foundation governance, no risk of future license changes.
- **If you're on a managed Redis service (ElastiCache, Upstash):** You may need to stay on redis-py for compatibility.
- **If you're just starting:** Use Valkey. It's a drop-in replacement and the community momentum is behind it.

**Lesson for your projects:** Infrastructure dependencies have licenses. When a dependency changes its license (Redis: BSD → SSPL, Elasticsearch: Apache → Elastic License, Terraform: MPL → BUSL), the community usually forks. Know which fork you're on and why.

---

## Testing as Architecture Enforcement

The test suite grew from 55 to 72 passing tests during this refactor. Each test category enforces a different architectural constraint:

### Security Tests
- `test_security_ssrf_guard.py` — 6 tests verifying IP blocking (loopback, RFC1918, link-local, AWS metadata)
- `test_security_input_sanitizer.py` — 4 tests verifying API key redaction, prompt injection filtering
- `test_security_exceptions.py` — 2 tests verifying error propagation
- `test_correlation_middleware.py` — 2 tests verifying correlation ID propagation

### Infrastructure Tests
- `test_stream_queue.py` — 6 tests with fakeredis: push/consume, consumer groups, DLQ, retry
- `test_record_repository.py` — 6 tests: CRUD, cursor pagination, empty, not-found, update, count
- `test_outbox_repository.py` — 6 tests: create/deliver/fail, idempotency, retry
- `test_extraction_schema.py` — 11 tests: schema validation, overlay CRUD

### Strategy Tests
- `test_override_strategy.py` — 6 tests: CSS selectors, img/link resolution, empty containers, identity hash
- `test_extraction_pipeline.py` — 7 tests: JSON-LD, overlay priority, schema validation, @graph expansion, semantic HTML

### Resilience Tests
- `test_resilience_identity_hash.py` — 5 tests: hash-based deduplication
- `test_resilience_oom_dlq.py` — 3 tests: payload limits, DLQ routing
- `test_resilience_turbo_guard.py` — 5 tests: turbo mode degradation
- `test_resilience_fanout_cap.py` — 2 tests: fan-out limits

### Compilation Check
Every commit runs `python -m py_compile` across all source files as a minimum gate.

**Lesson for your projects:** Tests are architecture documentation. A developer reading `test_stream_queue.py` learns how the stream consumer works without reading the implementation. A test that mocks 8 things signals an architecture problem. Tests that require real infrastructure (Redis, PostgreSQL) should be integration tests in a separate directory, not mixed with unit tests.

---

## Key Takeaways for Your Projects

1. **Ports before adapters.** Define what your application needs as protocols (`JobRepository`, `RecordRepository`). Implement them as adapters (`SqliteJobRepository`, `PostgresJobRepository`). Never let application code import infrastructure directly.

2. **State machines on everything.** Jobs, overlays, even individual records — anything that changes state across time needs explicit allowed transitions. A `can_transition_to()` method costs 10 lines and prevents entire classes of bugs.

3. **Outbox for cross-system consistency.** SQLite write + Valkey publish? Outbox. PostgreSQL write + Kafka publish? Outbox. The pattern is the same: write the event to the same database as the data, then relay it asynchronously.

4. **Schemas, not special cases.** The four entity types (Product, Opportunity, Lead, Article) were replaced with one `ExtractedRecord` + `ExtractionSchema`. A new data type is a new schema definition, not a new Python class.

5. **AI at the boundary.** AI is an infrastructure concern. Put it behind a protocol with a NoOp. Cache results at two levels. Circuit-break on failure. Never make AI a required path in your data pipeline.

6. **Record what happens.** `StrategyObservation` costs bytes, not dollars. After a thousand observations you can make evidence-backed decisions about which extraction strategies work. The alternative is guessing.

7. **Security audits are cheap early.** CORS wildcard+credentials, `ss_` auth bypass, and missing SSRF guard — all three were found and fixed in under 2 hours. Finding them after launch costs days of incident response.

8. **License your dependencies.** Redis → Valkey was a find-and-replace. But it only mattered because we checked. Know what license your infrastructure dependencies use. The BSD-to-SSPL transition pattern keeps repeating.
