# Integrating "Deep Research With Web Scraping by LLM and AI Agents" into Spacescraper

**Status:** Research / design proposal — no code changes yet.
**Subject:** `Deep-Research-With-Web-Scraping-by-LLM-And-AI-Agent-main/` (vendored in this repo since the initial commit, never wired into anything).
**Question:** which of its capabilities belong in Spacescraper, and where do they attach given Spacescraper's hexagonal architecture?

---

## 0. TL;DR

The vendored project is a **notebook-stage prototype**, not a library. Almost none of its code is
usable as-is. Its value is the **capability map in its README**, and against that map Spacescraper
already equals or beats it everywhere except in one place:

> **Spacescraper is URL-in. Deep Research is query-in.** There is no path in this repo from
> *"a question"* to *"a set of URLs"*. That single gap is the integration.

Everything else in the vendored project is either already implemented here in a stronger form
(fetching, storage, eval loop, persistence) or is a dependency-heavy re-implementation of something
this codebase deliberately does differently (free-running LLM agents vs. the human-gated overlay
lifecycle).

The recommended shape: **a new Discovery stage in front of the existing pipeline**, entered through
ports, with the existing SSRF guard, fan-out cap, rate limiter, observation loop, and SLO monitor
reused rather than duplicated. Increments 7–10 below add **zero new dependencies**.

---

## 1. What is actually in the vendored project

| Path | Lines | Contents |
|---|---|---|
| `src/AI-Agent/apps.py` | ~90 | Loads Gemma-2b-it / Llama-3-8B locally via `transformers` + 4-bit `BitsAndBytesConfig`; one `getResponse()` helper; a `print()` smoke test. Imports `crewai` but never uses it. |
| `src/dev/search-engine-test.ipynb` | ~20 cells | DuckDuckGo via `langchain_community`, then string-slicing the result blob into `{title, snippet, url}` dicts. |
| `src/dev/beautifulsoup_version.ipynb` | ~14 cells | `requests`/`aiohttp` + BS4 `find_all(tag)`; HF `pipeline('summarization')`. |
| `src/dev/selenium_version.ipynb` | ~10 cells | Headless Chrome, `driver.page_source`, tag text. |
| `src/dev/ai-agent-web-search-scraping -test.ipynb` | 3 cells | A `SearxSearchWrapper` import and a `crewai` import. Nothing implemented. |
| `src/dev/crawl4ai.py` | 0 | **Empty file.** |
| `src/ai-web-scraping-by-llm-dev.ipynb` | 5 cells | Imports, a `CFG` class of model names, a CUDA check. Nothing implemented. |
| `install-crawl4ai.sh` | 2 | `pip install -U crawl4ai` + `playwright install`. |
| `src/requirements.txt` | ~45 deps | Includes a typo'd `trainsformers` and `pytorch` (not a valid PyPI name). |

**Finding 1 — "integrate this" cannot mean "port this code."** There is no module here worth
importing. What follows treats the README's stated capabilities as the requirement set and asks,
per capability, whether Spacescraper should acquire it and where it attaches.

---

## 2. Capability map: Deep Research vs. Spacescraper today

| Deep Research capability | What Spacescraper has today | Verdict |
|---|---|---|
| **Search-engine URL discovery** (DuckDuckGo, Serper, SerpAPI, SearxNG) | **Nothing.** `POST /jobs` requires a concrete `HttpUrl`; `sources.yaml` seeds fixed `start_urls`; the only discovery is same-crawl `ProcessingResult.follow_urls`. | **ADOPT — the one real gap.** §3 |
| **crawl4ai as fetcher** | Playwright + `BrowserContextPool` + `stealth_brain` + `browser/persona` + `proxies/manager` + zero-browser turbo mode + `SmartCrawler` ETag/304 revalidation + `DomainRateLimiter`. | **REJECT.** A downgrade that forks the fetch path and bypasses the rate limiter, SSRF guard, stealth layer, and artifact store. |
| ...crawl4ai's *LLM-ready markdown output* | `DeterministicExtractionPipeline` goes HTML → `ExtractedRecord`. There is no "clean readable text" product; `RawScrapePayload.html_content` is the only text artifact. | **ADOPT the idea, not the dependency.** A `readable_text` artifact variant in the existing content-addressed store. |
| **BeautifulSoup / Selenium scraping** | BS4 is already core (`extraction_pipeline.py`). Selenium is a strict subset of Playwright. | **REJECT.** |
| **Local open LLMs** (Gemma, Llama 3, 4-bit quant) | `AIOrchestrator` hard-wires Gemini HTTP endpoints, with a circuit breaker + two-level `AICache`. An `EnrichmentProvider` ABC exists but is narrow (`enrich` / `is_available`) and `AIOrchestrator` does not implement it. | **ADOPT as an adapter**, after widening the port — but **not** by importing `torch` into a worker. §5 |
| **CrewAI / LangChain agents** | `extracted_scrapers/` already sketches a *deterministic, governed* agent set (ingestion, schema induction, selector repair, cost-aware planner, governance, deterministic runner) — orphaned, importing a `shared.contracts.*` package that does not exist in this tree. | **REJECT free-running agents.** §7. Adopt the *roles* into the existing governed pipeline. |
| **VectorDB** (FAISS / Chroma) | Gemini `text-embedding-004` vectors stored as JSON in a SQLite `TEXT` column, used only for cosine dedup via a linear scan; `_embedding_cache` is per-instance. | **ADOPT narrowly** — an ANN index behind a port, justified by dedup scaling, not by "RAG". §6 |
| **LLM evaluation** (TruLens, W&B, deepeval) | `StrategyEvaluator` → `EvaluationResult` → `DomainProfile`; `SLOMonitor` with auto-rollback; `ExplorationPolicy` (Thompson sampling, 5% bound); `FeedbackItem` as labeled data. Already a stronger harness than TruLens-in-a-notebook — but it scores **extraction**, never **LLM output**. | **ADOPT the missing metric, REJECT the framework.** §6 |
| **SQL DB for history** | SQLite + Postgres adapters, repository ports, outbox pattern, job state machine. | **REJECT** — far ahead already. |
| **Streamlit UI** | `GEMINI.md` documents `dashboard.py`; **the file does not exist in the tree.** | Out of scope — a separate gap, unrelated to this integration. |

---

## 3. The integration: a Discovery stage in front of the pipeline

Because Spacescraper's pipeline is URL-in and Deep Research is query-in, the integration is a **new
stage in front of the existing pipeline, not a change to it**. Downstream blast radius is zero:
the scraper, processor, and reporter never learn that a job came from a search result.

```
POST /research {query}                          ← NEW delivery surface
   │
   ▼
ResearchPlan  (src/domain/models.py)            ← NEW domain model
   │
   ▼
DiscoveryService  (src/application/)            ← NEW; depends on the SearchProvider PORT
   │   └── SearchProvider  (src/domain/ports.py)
   │          ├── DuckDuckGoSearchProvider   (httpx — no new dep)
   │          ├── SerperSearchProvider       (httpx + API key)
   │          └── NoOpSearchProvider         (default; feature dark-safe)
   ▼
SearchHit[] → validate_outbound_url() → SmartCrawler cache dedup → get_allowed_fanout()
   │
   ▼
existing Job + OutboxEvent + jobs_queue ──► scraper → processor → reporter   (UNCHANGED)
   │
   ▼
ExtractedRecord[] → SynthesisService (optional) → cited research answer
```

### Seam 1 — `SearchProvider` port

Add a `Protocol` to `src/domain/ports.py` beside `JobRepository` / `RecordRepository` /
`OverlayRepository`, returning a new pure domain model:

```python
class SearchHit(BaseModel):
    url: str
    title: str
    snippet: str
    rank: int
    provider: str
```

Adapters go in `src/infrastructure/providers/search_provider.py`, **mirroring the file next to it**
(`enrichment_provider.py`: ABC + `NoOp*` + a real adapter). Reuse `http_client`, `AICache`
(`provider="search"` — identical queries are common and search calls are billable), and
`DomainRateLimiter`.

*Why this shape:* it introduces no new pattern. A reviewer who knows `EnrichmentProvider` already
knows this. `NoOpSearchProvider` as the default keeps the feature dark without flag branches
scattered through the call sites.

### Seam 2 — reuse the queue envelope, do not invent one

`QueueMessage` already carries a `message_type` discriminator and `schema_version`. Add one member
to `MessageType`:

```python
DISCOVERY_QUERY = "discovery_query"
```

A discovery worker consumes `research_stream` and emits **ordinary `ScrapeJob`s** onto the existing
`jobs_queue`. Nothing downstream changes.

### Seam 3 — safety controls: reuse, never duplicate

This is the part that must not be improvised. **Search-derived URLs are user-influenced URLs**, the
same input class as `follow_urls` — an attacker who influences the query steers the crawler's
targets. Reuse the controls that already exist:

| Control | Existing helper | Applied where |
|---|---|---|
| SSRF | `validate_outbound_url()` (`src/security/ssrf_guard.py`) | Per hit, before enqueue. Remediation **A2** already asks for exactly this on discovered URLs. |
| Prompt injection / size | `sanitize_for_prompt()`, `validate_payload_size()` | On the query at the API edge — it reaches a third-party API and possibly an LLM. |
| Fan-out | `queue.get_allowed_fanout(root_id, n, MAX)` (atomic Lua, already used by `worker_processor` with `MAX_RECURSIVE_FANOUT = 200`) | A research query **is** a fan-out root. Cap it identically; overflow → DLQ with `reason="DISCOVERY_CAP_EXCEEDED"`. |
| Per-domain politeness | `DomainRateLimiter` | Unchanged — applies automatically once jobs hit the normal path. |
| Cost | `ai_cost_per_hour` SLO (warn 100 / crit 500, `slo_monitor.py`) | Query → N URLs multiplies job volume; the SLO already covers it. |

**Gap this exposes:** there is **no `robots.txt` handling anywhere in the repo** (verified by grep).
That is tolerable while every target comes from a curated `sources.yaml`; it stops being tolerable
the moment a search engine picks the domains. A robots check plus a settings-driven domain
allow/deny list is a **prerequisite** for Increment 7, not a follow-up.

### Seam 4 — determinism

Search results are not reproducible. Write the raw SERP response to the existing content-addressed
`artifact_store` (`artifacts/{sha256}`) and reference it from the `ResearchPlan`, so a research run
can be replayed against the exact result set it originally saw.

---

## 4. Where the LLM capabilities attach

### 4.1 Widen the enrichment port before adding any provider

`EnrichmentProvider` currently declares only `enrich()` and `is_available()`. `AIOrchestrator` —
which has `heal_selector`, `generate_overlay`, `enrich_opportunity`, `compute_embedding` — does not
implement it, and `main.py` imports the singleton directly. Remediation **B2** already flags this.

Order of work: widen the port (`generate`, `embed`, `generate_overlay`), make `AIOrchestrator`
implement it, inject it. Only then is any second provider a drop-in.

### 4.2 Local models: over HTTP, never in-process

**Do not import `torch` / `transformers` into a worker.** A 4-bit Gemma-2B resident in the scraper
process invalidates `BrowserContextPool` memory assumptions and the `pool_size` math, and makes the
workers effectively non-scalable horizontally.

Run the local model behind an OpenAI-compatible HTTP endpoint (Ollama, `llama.cpp` server, vLLM).
The adapter stays a thin `http_client` call; the circuit breaker, retry/backoff, and `AICache` all
keep working unchanged. Per the "Ponytail" ladder this needs **no new dependency at all** — which is
the strongest argument for it.

---

## 5. Vector storage — narrowly, and for the right reason

The honest justification is not "RAG". It is that dedup is currently a **linear scan over
JSON-decoded vectors**, which is a real scaling wall. So:

1. Do Remediation **C3** first — back the embedding cache with Redis, keep a small in-process L1.
   Already planned; no new dependency.
2. Add a `VectorIndex` port (`upsert(id, vector)` / `query(vector, k)`). Default adapter = today's
   linear scan, so behaviour is unchanged. A FAISS or Chroma adapter becomes worthwhile **only**
   once a domain crosses a record-count threshold, gated behind `features["vector_index"]`.

That ordering means the dependency is added when a measurement demands it, not on spec.

---

## 6. LLM evaluation: the loop already exists

This is the most valuable finding in the review. The Deep Research README asks for TruLens/W&B to
measure content relevance, answer relevance, accuracy, recall, precision. Spacescraper **already
has that loop, better engineered** — `StrategyObservation` → `StrategyEvaluator` → `EvaluationResult`
→ `DomainProfile`, with `SLOMonitor` auto-rollback and a bounded `ExplorationPolicy`. It is missing
exactly one thing: **the LLM's own output is never scored.**

Close it inside the existing tables — `StrategyObservation.strategy` is already a free string:

1. Record observations with `strategy="llm_extract"` / `"llm_synthesis"` from the LLM paths.
2. Add two nullable fields to `StrategyObservation`: `groundedness` (fraction of LLM claims
   traceable to a source record or SERP snippet) and `citation_coverage`. Both are computable from
   string/embedding overlap against the source artifact — which the content-addressed store already
   retains. **No evaluation framework required.**
3. `StrategyEvaluator` then scores LLM strategies with the same `score` / `recommendation`
   machinery; add an `llm_groundedness` SLO so a regressing prompt or model auto-rolls back;
   `ExplorationPolicy` bounds how often the LLM path is tried at all (5% default).

Net effect: adopt the *metric* the vendored project wants, reject the four dependencies
(`trulens`, `wandb`, `deepeval`, `evaluate`) it proposes to get it.

### Synthesis output

A research answer is a new **artifact type**, not a new pipeline. `SynthesisService` reads the
`ExtractedRecord`s for a `root_job_id`, asks the LLM for an answer with per-claim `record_id`
citations, writes it to `artifact_store`, and emits the existing `DiscoveryEvent` — which the
reporter already fans out to Slack, webhooks, and file exports. Mandatory citations are what make
the groundedness metric above computable, so the two designs reinforce each other.

---

## 7. Explicit rejections

1. **Do not adopt the dependency set.** ~45 packages (`crewai`, `langchain`, `torch`,
   `transformers`, `faiss-cpu`, `chromadb`, `wandb`, `trulens`, …) against a current core of ~30
   focused ones. It would multiply image size and directly contradict "Ponytail" rung 4 (*does an
   already-installed dependency solve it?*). In every case above, one does.

2. **Do not let agents mutate selectors at runtime.** The overlay lifecycle is
   `CANDIDATE → SHADOW → ACTIVE → RETIRED` with `/overlays/{id}/promote` requiring
   `human_approved=True` for ACTIVE. This repo's own `extracted_scrapers/governance_agent.py` states
   the rule outright: *"Never activates selectors automatically."* A CrewAI crew that rewrites
   selectors mid-run deletes the shadow-evaluation and auto-rollback safety net that Increments 5–6
   were built to provide.

3. **Do not fork the fetch path** with crawl4ai or Selenium — it bypasses `DomainRateLimiter`,
   `stealth_brain`, personas, the proxy manager, the SSRF guard, and the artifact store.

4. **Do not call the search API inside the request handler.** It is a third-party network call with
   an unpredictable p95; `POST /jobs` is `202`-async for precisely this reason. Discovery belongs in
   a worker.

5. **Do not let LLM-derived fields reach `identity_hash`.** GEMINI.md convention #2 — search
   snippets and LLM summaries are content-hash-side only, or the next prompt change triggers a
   false-discovery storm.

6. **Do not leave the vendored tree unwired.** Remediation Phase E counts orphaned parallel
   architectures as a score-capping anti-pattern, and this directory (1.1 MB of notebooks) plus
   `extracted_scrapers/` (imports a non-existent `shared.contracts` package) are two of them.
   Recommendation: this document captures everything of value in the vendored tree — **delete it**,
   and either wire or delete `extracted_scrapers/` in the same pass.

---

## 8. Incremental delivery

Following the repo's existing `Increment N` commit convention.

| # | Increment | New deps | Notes |
|---|---|---|---|
| **7** | **Discovery.** `SearchProvider` port + DuckDuckGo adapter + `NoOp` default; `ResearchPlan` model; `POST /research`; discovery worker; `MessageType.DISCOVERY_QUERY`; SSRF + fan-out + rate-limiter reuse; robots.txt + domain allow/deny list; SERP archived to `artifact_store`. | **none** (`httpx` present) | Prerequisite: robots handling. Highest value — closes the only true capability gap. |
| **8** | **Provider port widening** (`generate` / `embed` / `generate_overlay`); `AIOrchestrator` implements it; local-LLM-over-HTTP adapter. | **none** | Also discharges Remediation B2. |
| **9** | **LLM output quality.** `llm_*` strategy observations; `groundedness` + `citation_coverage` fields; `llm_groundedness` SLO + auto-rollback; exploration bound. | **none** | Reuses the entire Increment 5–6 loop. |
| **10** | **Synthesis.** `SynthesisService`; cited answer artifact; `DiscoveryEvent` emission. | **none** | Citations are what make #9 measurable. |
| **11** | **Vector index** (conditional). `VectorIndex` port; linear-scan default; FAISS adapter behind `features["vector_index"]`. | 1, optional | Only once measurement shows dedup scan cost matters. |

Increments 7–10 add **no new dependencies**. That is a checkable claim, and the main reason this
plan is compatible with the project's stated philosophy.

---

## 9. Risks and open questions

- **Search provider terms.** Scraping DuckDuckGo HTML and using SerpAPI/Serper under licence are
  materially different postures. The port makes the provider swappable; the decision is a policy
  one and needs an owner.
- **Unvetted domains.** Curated `sources.yaml` targets are trusted in a way search results are not.
  The robots + allow/deny list in Increment 7 is load-bearing, not optional polish.
- **Cost multiplication.** One query fans out to N jobs, each potentially LLM-touched. The
  `ai_cost_per_hour` SLO covers it, but the discovery fan-out cap should start well below the
  crawl cap of 200.
- **Non-determinism.** Archiving the SERP addresses replay; it does not make two runs of the same
  query comparable. Evaluation comparisons should be scoped per `ResearchPlan`, not across plans.
- **Pre-existing defects noticed during this review** (unrelated to the integration, but they sit on
  the code paths it would touch): `main.py` calls `asyncio.create_task` without importing `asyncio`,
  annotates `RecordsResponse.records` with `List` without importing it, and branches on
  `OverlayState.CANARY`, which is not a member of that enum.
