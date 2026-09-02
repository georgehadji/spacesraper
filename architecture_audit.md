# Architecture Audit — Spacescraper

**Protocol:** ARCH-AUDIT-V2  
**Codebase:** 58 source modules, 9,579 lines, 72 passing tests  
**Date:** 2026-07-17

---

## Phase 1: Architectural Fingerprinting

### Detected Architecture: **Hybrid Hexagonal + Pipeline**

The codebase follows a **hexagonal (ports-and-adapters) architecture** for the data/repository layer combined with a **pipeline architecture** for the extraction/processing workflow. This is a detected pattern — inferred from actual import dependencies and module organization, not from documentation.

**Supporting evidence:**

1. **Domain layer is infrastructure-free** [VERIFIED]: `src/domain/` contains 2 files (`models.py`, `ports.py`) with zero infrastructure imports. All domain models are pure Pydantic `BaseModel` subclasses. All ports are abstract `Protocol` classes.

2. **Infrastructure adapters depend on domain** [VERIFIED]: `src/infrastructure/repositories/` contains 5 repository implementations (`SqliteJobRepository`, `SqliteRecordRepository`, `SqliteOutboxRepository`, `SqliteOverlayRepository`, `SqliteObservationRepository`). All import from `src.domain.models` — never the reverse.

3. **Composition roots in entry points** [VERIFIED]: `main.py`, `worker_scraper.py`, `worker_processor.py`, `worker_reporter.py` instantiate concrete adapters and wire them together. This is the hexagonal composition root pattern.

4. **Pipeline chain for extraction** [VERIFIED]: `src/application/extraction_pipeline.py` is a sequential strategy chain: overlay → JSON-LD → semantic HTML. This is distinct from the hexagonal pattern — it's a pipeline.

5. **Event-driven async workers** [VERIFIED]: Workers consume from Valkey Streams via `consume()` callback pattern — event-driven topology.

### Dependency Graph (inferred from imports)

```
main.py + workers (composition roots)
  ├── src/domain/models.py (pure models)
  ├── src/domain/ports.py (pure protocols)
  ├── src/application/extraction_pipeline.py
  │     └── src/infrastructure/repositories/overlay_repository.py  ← LEAK
  ├── src/application/evaluator.py
  │     └── src/infrastructure/repositories/observation_repository.py  ← LEAK
  └── src/infrastructure/repositories/*.py
        └── src/domain/models.py ✓
```

### Data Flow Topology

```
HTTP → FastAPI → Job (SQLite) + Outbox (SQLite) → Valkey Streams → Workers → Valkey Streams → Reporter
                                                                      ↓
                                                                   Records (SQLite)
```

---

## Phase 2: Compliance Matrix

| Module | Detected Pattern | Intended Pattern | Drift | Violations | Severity | Evidence |
|--------|-----------------|-----------------|-------|------------|----------|----------|
| **src/domain/** | Pure domain models | Pure domain models | None | None | — | Zero infrastructure imports [VERIFIED] |
| **src/application/pipeline.py** | Fat orchestrator | Lean use-case service | Legacy — imports `ai_orchestrator` directly | Infrastructure leakage | **HIGH** | `from src.infrastructure.ai.client import ai_orchestrator` [VERIFIED] |
| **src/application/post_processor.py** | State auditor | Should use repository port | Imports `intel_tracker` singleton directly | Infrastructure leakage | **HIGH** | `from src.infrastructure.storage.sqlite_tracker import intel_tracker` [VERIFIED] |
| **src/application/evaluator.py** | Use-case service | Should receive repo via DI | Imports `SqliteObservationRepository` directly | Infrastructure leakage | **MEDIUM** | [VERIFIED] |
| **src/application/extraction_pipeline.py** | Pipeline step | Should receive repo via DI | Imports `SqliteOverlayRepository` directly | Infrastructure leakage | **MEDIUM** | [VERIFIED] |
| **src/application/shadow_evaluator.py** | Use-case service | Should receive repo via DI | Imports `SqliteOverlayRepository` directly | Infrastructure leakage | **MEDIUM** | [VERIFIED] |
| **src/application/strategy_selector.py** | Background service | Should receive repo via DI | Imports `SqliteObservationRepository` directly | Infrastructure leakage | **MEDIUM** | [VERIFIED] |
| **src/infrastructure/queues/** | Queue adapters | Queue adapters | None | None | — | [VERIFIED] |
| **src/infrastructure/repositories/** | SQLite adapters | SQLite adapters | None | None | — | [VERIFIED] |
| **src/security/** | Security guards | Security guards | None | None | — | [VERIFIED] |

---

## Phase 3: Dependency & Coupling Analysis

### Circular Dependencies
**None detected.** [VERIFIED] — No module in `src/` imports another module that imports it back. This is confirmed by the import graph analysis.

### Layer Leaks (Application → Infrastructure)
**6 instances.** [VERIFIED] — All in `src/application/` importing from `src/infrastructure/`:

| File | Imports | Severity | Status |
|------|---------|----------|--------|
| `pipeline.py` | `ai_orchestrator` | HIGH | Legacy, not actively used |
| `post_processor.py` | `intel_tracker` | HIGH | Legacy, not actively used |
| `evaluator.py` | `SqliteObservationRepository` | MEDIUM | New code, should use DI |
| `extraction_pipeline.py` | `SqliteOverlayRepository` | MEDIUM | New code, should use DI |
| `shadow_evaluator.py` | `SqliteOverlayRepository` | MEDIUM | New code, should use DI |
| `strategy_selector.py` | `SqliteObservationRepository` | MEDIUM | New code, should use DI |

The 2 HIGH findings are in legacy pipeline code that is being phased out. The 4 MEDIUM findings are in new hexagonal code that correctly depends on ports but uses concrete imports as a shortcut instead of constructor injection.

### Shared Mutable State Risks
- **ObservabilityMetrics** (`src/infrastructure/monitoring/observability.py`): Uses a module-level singleton (`metrics_tracker`) with a local mutable `_local_cache` dict. Access is guarded by `asyncio.Lock`, but the cache is not Valkey-backed — if multiple worker processes use it, the local cache diverges. [HYPOTHESIS — sufficient for development, risk for production]

### Tight Coupling Hotspots
- **`src/domain/models.py`** (155 lines): Contains 15+ models across 5 concerns (job lifecycle, queue messages, extraction schemas, overlays, learning models). High afferent coupling — almost everything imports from it. This is acceptable for a domain module. [VERIFIED]

### Boundary Violations
- **`worker_processor.py:95`**: Uses `getattr(payload, 'overlay', None)` to access fields that may not exist on `RawScrapePayload`. Before Phase 0's `overlay` field addition, this was dynamic attribute injection. After the fix, these fields exist, but the `getattr` pattern suggests uncertainty about the model contract. [VERIFIED — mitigated by model field addition]

---

## Phase 4: AI Orchestrator Review

**N/A — not an AI orchestration project.** The project uses AI as an optional, non-critical enrichment adapter (`GeminiEnrichmentProvider`, `AIOrchestrator`). There is no LLM orchestration, agent pipeline, or multi-model routing. The AI calls are:
- `generate_overlay()` — optional, wraps Gemini API with circuit breaker
- `enrich_opportunity()` — legacy, not actively used
- `GeminiEnrichmentProvider` — clean adapter behind an ABC protocol

The AI path is properly isolated behind the `EnrichmentProvider` port and `AICache` for caching. No orchestration concerns. [VERIFIED]

---

## Phase 5: Anti-Pattern Detection

### Detected: Infrastructure Leakage (Application Layer)
**Severity: MEDIUM** — 6 instances from Phase 3. The application use-case services should receive infrastructure adapters via constructor injection, not import them directly. This makes testing harder (cannot mock the repository) and couples the application layer to the persistence technology.

### Detected: Singleton Managers
**Severity: LOW** — `ai_orchestrator`, `metrics_tracker`, `intel_tracker`, `api_key_manager` are all module-level singletons with global state. While convenient for a single-process service, this prevents clean teardown and creates hidden coupling between components that share the same state.

### Detected: Legacy Pipeline Fat Module
**Severity: MEDIUM** — `src/application/pipeline.py` (270 lines) handles extraction, enrichment, deduplication, and audit. It's a fat orchestrator that violates the Single Responsibility Principle. However, it is legacy code that is being phased out — the new `DeterministicExtractionPipeline` replaces its extraction function, and the `Opportunity`-specific deduplication is deprecated.

### Not Detected (code is clean for these):
- God service / God module ✅ (domain models are focused, workers are lean)
- Hidden monolith ✅ (clear module boundaries)
- Shared database coupling ✅ (single SQLite file is by design for Phase 0–2)
- Temporal coupling ✅ (async events decouple execution)
- Anemic domain model ✅ (models carry validation logic — `Job.transition_to()`, `ExtractionSchema.validate_record()`)
- Orchestrator bottleneck ✅ (no single coordinator — API, scraper, processor, reporter are independent processes)

---

## Phase 6: Executive Summary

**ARCHITECTURE SCORE: 7 / 10**

**Maturity Level: Early Production**

**Scoring breakdown:**
- Domain layer pure: +2 (perfect hexagonal)
- Infrastructure adapters correct: +2
- Application layer leaks: -1 (4 MEDIUM leaks in new code)
- Legacy pipeline coupling: -1
- Singleton patterns: -0.5
- No circular dependencies, clean composition roots: +1
- Testable: -0.5 (direct imports prevent mocking; integration tests blocked by conftest)
- Documentation: +0.5 (README updated, API documented)
- Security boundary enforcement: +0.5 (SSRF/CORS/auth all wired)
- Observability: +0.5 (SLO monitor, metrics, logging)

**PRIMARY RISKS (ranked by impact):**
1. **Application-layer infrastructure leakage** (MEDIUM) — 4 new modules import concrete adapters instead of receiving them via DI. Hinders testability and violates hexagonal contract.
2. **Singleton managers** (LOW) — Global state in `ai_orchestrator`, `metrics_tracker`, `intel_tracker`. Creates hidden coupling; prevents clean parallel test execution.
3. **Single SQLite database** (LOW) — All 5 repository adapters share one SQLite file (`spacescraper_jobs.db`). Under concurrent write load, WAL-mode contention is possible. Plan calls for PostgreSQL adapter.
4. **Test infrastructure fragility** (LOW) — `conftest.py` event_loop fixture incompatibility with pytest-asyncio 0.24 prevents running integration tests via pytest on Windows.

**CRITICAL VIOLATIONS:** None.

**REFACTOR URGENCY: Next Sprint**
Justification: The 4 MEDIUM layer leaks in new code should be fixed before the codebase grows more hexagonal modules, as fixing them later will require changing every new use-case service. Legacy pipeline leaks can remain until the legacy code is fully removed.

---

## Phase 7: Refactoring Roadmap

### IMMEDIATE (fix before next feature)

| Finding | Action | Expected Outcome |
|---------|--------|-----------------|
| 4 MEDIUM layer leaks | Replace `from src.infrastructure.repositories...` imports with constructor injection in `evaluator.py`, `extraction_pipeline.py`, `shadow_evaluator.py`, `strategy_selector.py` | All application modules accept repos via `__init__`, enabling unit testing with mocks |
| Conftest event_loop | Use `asyncio_mode = "auto"` in pytest.ini (done) | Integration tests runnable via `pytest` on all platforms |

### HIGH-IMPACT (next sprint)

| Finding | Action | Expected Outcome |
|---------|--------|-----------------|
| 2 HIGH legacy leaks | Remove unused legacy pipeline code (`src/application/pipeline.py`, `src/application/post_processor.py`) after verifying all consumers are migrated | Remove 2 layer violations and ~400 lines of dead code |
| Singleton managers | Replace module-level `ai_orchestrator` with DI — pass to consumers via `__init__` | AIOrchestrator becomes mockable in tests |

### LONG-TERM (architectural evolution)

| Step | Action | Risk |
|------|--------|------|
| 1 | Introduce `src/bootstrap.py` composition root — move all adapter instantiation out of `main.py` and workers | LOW — pure refactor, no behavior change |
| 2 | Replace single SQLite with PostgreSQL adapter behind same repository ports | MEDIUM — requires migration scripts; no API changes |
| 3 | Remove legacy `Opportunity`/`Product`/`Lead`/`Article` models | LOW — all new paths use `ExtractedRecord` |
| 4 | Migrate from `asyncio.gather` LIST+Stream dual consumers to Streams-only consumer | LOW — removes the old `RedisQueueWorker` entirely |

### Switching Triggers
- **Valkey cluster failure**: If Valkey becomes unavailable, the system currently degrades to `fakeredis` in-memory mode (with data loss on restart). Production deployment should provide a Valkey Sentinel/cluster.
- **PostgreSQL requirement**: When the single SQLite file reaches write contention, switch to the PostgreSQL adapter behind the same repository interfaces.
- **Multi-process scaling**: When 2+ scraper workers run concurrently, the `_turbo_miss_counts` and `hybrid_registry` per-process state becomes inaccurate — move to Valkey-backed shared state.
