# Architecture Remediation Plan — Spacescraper

**Based on:** Architecture Audit (ARCH-AUDIT-V2, Score 7/10)  
**Date:** 2026-07-17  
**Status:** Draft — Ready for implementation

---

## Objective

Raise the architecture score from **7/10 → 9/10** by resolving all MEDIUM and HIGH violations, eliminating legacy coupling, and hardening testability. Target: Production-ready hexagonal architecture with zero application-layer infrastructure leaks and testable use-case services.

---

## Design Principles

1. **Hexagonal contract**: Application services receive infrastructure adapters via constructor injection — never import them directly.
2. **Port-first**: Every new dependency that crosses a layer boundary must have a protocol port in `src/domain/ports.py`.
3. **One composition root**: All adapter instantiation and wiring lives in a single `src/bootstrap.py` module, not scattered across `main.py` and workers.
4. **Backward compatible**: No API contract changes. No worker behavior changes. Pure structural refactoring.
5. **Incrementally testable**: Each refactored module gets a unit test with mocked dependencies before the module is considered complete.
6. **Legacy phase-out**: Marked-for-removal code is cleaned up, not refactored in-place.

---

## Phase A: Application-Layer Infrastructure Leak Fixes (MEDIUM)

**Goal:** Eliminate ALL application-layer imports of infrastructure modules. Every use-case service receives its dependencies via `__init__`.

### A1 — `src/application/extraction_pipeline.py`

**Current:** Imports `SqliteOverlayRepository` directly at line ~10.  
**Target:** `DeterministicExtractionPipeline.__init__` receives `overlay_repo: Optional[OverlayRepository] = None`.

| Step | File | Change |
|------|------|--------|
| A1.1 | `src/domain/ports.py` | Verify `OverlayRepository` protocol is clean (already done) |
| A1.2 | `src/application/extraction_pipeline.py` | Change `from src.infrastructure...` to `from src.domain.ports import OverlayRepository`. Remove direct import of `SqliteOverlayRepository`. Update `__init__` parameter type. |
| A1.3 | `main.py` | Pass `overlay_repo` instance to pipeline constructor |
| A1.4 | `tests/test_extraction_pipeline.py` | Add test with mocked `OverlayRepository` |

### A2 — `src/application/evaluator.py`

**Current:** Imports `SqliteObservationRepository` directly.  
**Target:** `StrategyEvaluator.__init__` receives `repo: ObservationRepositoryPort`.

| Step | File | Change |
|------|------|--------|
| A2.1 | `src/domain/ports.py` | Add `ObservationRepository` protocol with `get_observations`, `create_evaluation`, `get_or_create_profile`, `update_profile` |
| A2.2 | `src/application/evaluator.py` | Replace `SqliteObservationRepository` import with `ObservationRepository` protocol import. Update `__init__` parameter type. |
| A2.3 | `main.py` + `src/application/strategy_selector.py` | Pass `obs_repo` to evaluator constructor |
| A2.4 | `tests/test_increment_modules.py` | Update evaluator test to use mock repo |

### A3 — `src/application/shadow_evaluator.py`

**Current:** Imports `SqliteOverlayRepository` directly.  
**Target:** `ShadowOverlayEvaluator.__init__` receives `overlay_repo: OverlayRepository`.

| Step | File | Change |
|------|------|--------|
| A3.1 | `src/application/shadow_evaluator.py` | Replace `SqliteOverlayRepository` import with `OverlayRepository` protocol. Update `__init__`. |
| A3.2 | `main.py` | Pass `overlay_repo` instance to shadow evaluator |

### A4 — `src/application/strategy_selector.py`

**Current:** Imports `SqliteObservationRepository` directly.  
**Target:** `StrategySelector.__init__` receives `obs_repo: ObservationRepository`.

| Step | File | Change |
|------|------|--------|
| A4.1 | `src/application/strategy_selector.py` | Replace `SqliteObservationRepository` import with protocol import. Update `__init__`. |
| A4.2 | `main.py` | Pass `obs_repo` to strategy selector constructor |

**Exit gate:** `python -c "import ast; ..."` confirms zero `src.infrastructure` imports in `src/application/`. All 4 modules have DI-based constructors. Tests pass.

---

## Phase B: Legacy Pipeline Cleanup (HIGH)

**Goal:** Remove the two HIGH-severity legacy layer leaks by cleaning up dead procurement code.

### B1 — Remove `src/application/pipeline.py` dependency on `ai_orchestrator`

**Current:** `DataPipeline` imports `ai_orchestrator` singleton from infrastructure. The `_enrich_opportunity` method is the sole consumer, and it was neutered in Phase 0 (embedding computation removed).  
**Target:** Remove the `ai_orchestrator` import and the dead `_enrich_opportunity` path entirely.

| Step | File | Change |
|------|------|--------|
| B1.1 | `src/application/pipeline.py` | Remove `from src.infrastructure.ai.client import ai_orchestrator`. Remove `_enrich_opportunity` method (lines 96–111). Remove the `await self._enrich_opportunity(entity)` call in `process()`. |
| B1.2 | `worker_processor.py` | Disable `ai_enrichment_enabled=True` in `DataPipeline()` constructor (set to `False`) |
| B1.3 | `tests/test_resilience_identity_hash.py` | Verify tests still pass — they already use `ai_enrichment_enabled=False` |

### B2 — Remove `src/application/post_processor.py` dependency on `intel_tracker`

**Current:** `IntelligencePostProcessor` imports `intel_tracker` singleton. Post-processor is called only from `worker_processor.py` and is Opportunity-specific (legacy).  
**Target:** Either remove entirely or convert to DI.

| Step | File | Change |
|------|------|--------|
| B2.1 | `src/application/post_processor.py` | Replace `from src.infrastructure.storage.sqlite_tracker import intel_tracker` with `intel_tracker` parameter in `__init__` |
| B2.2 | `worker_processor.py` | Pass `intel_tracker` to `IntelligencePostProcessor(intel_tracker)` |
| B2.3 | `src/application/classifier.py` | Delete — was already neutered to a comment in Phase 0. Remove file. |

**Exit gate:** `src/application/pipeline.py` and `src/application/post_processor.py` have zero `src.infrastructure` imports. Identity hash tests pass. Classifier file deleted.

---

## Phase C: Singleton Manager Cleanup (LOW)

**Goal:** Replace module-level singletons with DI where they cross process boundaries. Leave ones that are process-local (acceptable for development).

### C1 — `ai_orchestrator` (Legacy AI Client)

| Step | File | Change |
|------|------|--------|
| C1.1 | `main.py` | Move `ai_orchestrator` instantiation to `bootstrap.py`. Pass to `/autograph` handler via FastAPI `Depends` or module-level kept for simplicity. |
| C1.2 | `src/infrastructure/ai/client.py` | Remove `ai_orchestrator = AIOrchestrator()` module-level singleton. |

**Decision:** Keep `ai_orchestrator` as a module-level instance for now — it's only used in one endpoint (`/autograph`) and is process-local. Mark as `# TODO: migrate to DI in bootstrap.py`.

### C2 — `metrics_tracker`

Keep as-is. It's a process-local singleton with thread-safe access. Not a boundary violation.

### C3 — `intel_tracker`

Addressed in Phase B2 — replaced with DI.

### C4 — `api_key_manager`

Keep as-is. It's a process-local singleton initialized in lifespan. Clean pattern for an in-memory key store.

**Exit gate:** No remaining unaddressed singleton concerns. All singletons have a documented disposition (keep, migrate, or remove).

---

## Phase D: Bootstrap Module (Composition Root)

**Goal:** Create a single `src/bootstrap.py` that centralizes all adapter instantiation. `main.py` and workers import from bootstrap, not from concrete adapters.

| Step | File | Change |
|------|------|--------|
| D1 | `src/bootstrap.py` | Create new file. Instantiate all 5 repositories, rate limiter, artifact store, stream queue, enrichment provider, cache, evaluator, strategy selector, SLO monitor. Export as typed variables. |
| D2 | `main.py` | Replace all adapter instantiation with imports from `bootstrap`. Example: `from src.bootstrap import job_repo, record_repo, outbox_repo, obs_repo, overlay_repo, slo_monitor, strategy_selector` |
| D3 | `worker_scraper.py` | Replace `SqliteJobRepository()` with `from src.bootstrap import job_repo, rate_limiter, artifact_store, obs_repo` |
| D4 | `worker_processor.py` | Replace `SqliteJobRepository()` + `SqliteRecordRepository()` with bootstrap imports |
| D5 | `worker_reporter.py` | Replace `RedisStreamQueue()` with bootstrap import |

**Exit gate:** `git grep "Sqlite.*Repository()"` returns ZERO results outside `src/bootstrap.py`. All bootstrap variables are typed with their protocol/ABC types.

---

## Phase E: Test Infrastructure & Coverage

**Goal:** Fix conftest, add DI-enabled tests for refactored modules.

| Step | File | Change |
|------|------|--------|
| E1 | `tests/conftest.py` | Keep current clean version (no event_loop fixture). Add `pytestmark = pytest.mark.asyncio(scope="function")` if needed. |
| E2 | `tests/test_extraction_pipeline.py` | Add `test_pipeline_with_mock_overlay_repo()` — passes a mocked `OverlayRepository` to `DeterministicExtractionPipeline` |
| E3 | `tests/test_evaluator.py` | Extract evaluator tests from `test_increment_modules.py` into dedicated file. Add `test_evaluator_with_mock_repo()` |
| E4 | `tests/test_shadow_evaluator.py` | New file — tests `ShadowOverlayEvaluator` with mocked overlay repo and evaluator |
| E5 | `tests/test_artifact_store.py` | Verify existing 6 tests pass |

**Exit gate:** All tests pass via `python -m pytest tests/` (integration tests may need `asyncio_mode=auto` override on Windows).

---

## Phase F: Final Cleanup & Verification

| Step | Action | Evidence |
|------|--------|----------|
| F1 | Run full test suite | 72+ tests pass |
| F2 | Verify zero infrastructure imports in `src/application/` | `grep -r "src.infrastructure" src/application/` returns empty |
| F3 | Verify bootstrap module compiles and exports all adapters | `python -m py_compile src/bootstrap.py` |
| F4 | Commit and push | Git log with clean, documented commits |
| F5 | Re-run architecture audit | Score should be 8–9/10 |

---

## Sequencing & Dependencies

```
Phase A (DI leaks) → Phase B (legacy cleanup) → Phase C (singletons) → Phase D (bootstrap) → Phase E (tests) → Phase F (verification)
     ↓
All phases are independent of each other in terms of code changes,
but Phase D depends on A-C being complete to know what to import.
Phase E depends on A to have mockable constructors.
```

---

## Effort Estimate

| Phase | Changes | Files | Estimated Time |
|-------|---------|-------|---------------|
| A — Application DI leaks | 4 protocol imports, 4 __init__ updates | 8 | 30 min |
| B — Legacy cleanup | Remove 2 imports, 1 method, 1 file | 4 | 20 min |
| C — Singleton disposition | Document decisions, 1 minor change | 2 | 10 min |
| D — Bootstrap module | New file, update 5 entry points | 6 | 20 min |
| E — Test infrastructure | New tests, conftest polish | 5 | 20 min |
| F — Final verification | Compile, test, audit | — | 10 min |
| **Total** | | **25 files** | **~2 hours** |

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| DI change breaks existing test | LOW | LOW | All existing tests use `ai_enrichment_enabled=False` — no new coverage needed |
| Bootstrap module causes circular imports | LOW | MEDIUM | Bootstrap only imports from infrastructure layer (no reverse dependencies) |
| Legacy pipeline removal breaks processor | LOW | HIGH | `DataPipeline.process()` is still called from worker; removing `_enrich_opportunity` is safe (already a no-op at runtime) |
| Overlay repo protocol mismatch | LOW | LOW | `SqliteOverlayRepository` already matches the intended `OverlayRepository` protocol — just formalizing it |

---

## Target Architecture (Post-Fix)

```
src/domain/
  models.py          — pure Pydantic models
  ports.py           — JobRepository, RecordRepository, OutboxRepository,
                        OverlayRepository, ObservationRepository,
                        ArtifactStore (all Protocol classes)

src/application/
  extraction_pipeline.py  — depends on OverlayRepository (protocol) ✓
  evaluator.py            — depends on ObservationRepository (protocol) ✓
  shadow_evaluator.py     — depends on OverlayRepository (protocol) ✓
  strategy_selector.py    — depends on ObservationRepository (protocol) ✓
  exploration_policy.py   — depends on nothing ✓

src/bootstrap.py           — SINGLE composition root
  Instantiates all adapters, wires DI

main.py, worker_*.py       — import from bootstrap, no adapter instantiation
```
