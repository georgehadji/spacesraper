# Google Maps Scraper → Spacescraper: Integration Analysis

## 1. WHAT EACH PROJECT BRINGS

### google-maps-scraper (Go/Playwright)
- **Core capability:** Extracts 36-field business listing data from Google Maps search results
- **Runtime:** Headless Chromium via Playwright for Go
- **Scale:** ~120 results per search; grid mode subdivides geography to bypass limits
- **Output:** CSV, JSON, PostgreSQL, S3, LeadsDB
- **Source code:** ~31,000 lines of Go across 80+ files
- **Tests:** 17 test files

### Spacescraper (Python/FastAPI)
- **Core capability:** Generic web extraction with schema-driven pipeline
- **Runtime:** Playwright for Python (already installed), httpx for static pages
- **Scale:** Distributed via Valkey Streams + 3-worker topology
- **Output:** `ExtractedRecord` → SQLite → CSV/JSON artifacts
- **Source code:** ~57 Python modules
- **Tests:** 72 tests, 55+ stable

---

## 2. THREE INTEGRATION APPROACHES

### Approach A: Sidecar Service (HTTP Bridge)
Run google-maps-scraper as a separate Go process. Spacescraper calls it over HTTP.

```
Spacescraper API → worker_scraper → HTTP call → google-maps-scraper → CSV → parse CSV → ExtractedRecord
```

**Pros:** Zero Go code changes, language-agnostic, quick to wire up.
**Cons:**
- Two deployments, two monitoring stacks, two failure domains
- google-maps-scraper CLI has no persistent API mode (web UI mode uses SQLite)
- Results come back as CSV/JSON files, need re-parse → no schema validation, no identity hashing
- No integration with Spacescraper's overlay lifecycle, strategy selection, or observation recording
- Go binary must be compiled for deployment platform
- **Architectural mismatch:** google-maps-scraper's job model (query → scroll → parse → write file) doesn't fit Spacescraper's pipeline (job → extract → validate → record → observe)

### Approach B: Python Reimplementation (Native Strategy)
Port the extraction knowledge into a Spacescraper `GoogleMapsStrategy` + `ExtractionOverlay`.

```
Spacescraper API → worker_scraper → ScraperEngine(Playwright) → GoogleMapsStrategy → ExtractedRecord → validate → record
```

**Pros:**
- Single codebase, single deployment, single monitoring
- Reuses Spacescraper's existing Playwright infrastructure (engine, pool, persona, stealth)
- Extraction results go through the full pipeline: schema validation, identity hashing, change detection, artifact writing
- Overlay lifecycle (CANDIDATE → SHADOW → ACTIVE) allows safe rollouts
- StrategyObservation records every extraction for evaluation
- Grid-based search can be implemented as fan-out jobs (already supported via `MAX_RECURSIVE_FANOUT`)
- Shared rate limiter, AI cache, and artifact store

**Cons:**
- Must port ~3,000 lines of parsing logic from Go to Python
- Google Maps DOM selectors are fragile — needs maintenance
- Playwright overhead (headless Chromium) per extraction

### Approach C: gRPC Microservice
Wrap google-maps-scraper in a gRPC server. Spacescraper calls it as a remote provider.

**Pros:** Typed contracts, streaming, high performance.
**Cons:** Same operational overhead as Approach A plus gRPC complexity. Two runtimes.

---

## 3. RECOMMENDATION: APPROACH B — Native Strategy Port

**Verdict:** Spacescraper was specifically designed to absorb exactly this kind of specialized extractor. The architecture already has:

| Already Exists in Spacescraper | google-maps-scraper Equivalent |
|---|---|
| `ScraperEngine` (Playwright) | Playwright for Go |
| `BrowserContextPool` | Internal browser pool |
| `persona_manager` (stealth UA/WebGL) | No equivalent (Places API avoidance) |
| `DeterministicExtractionPipeline` | Hardcoded in `gmaps/job.go` |
| `ExtractionOverlay` + `ExtractionSchema` | No equivalent |
| `ExtractedRecord` (generic) | `gmaps.Entry` (36 specific fields) |
| `StrategyObservation` + `ShadowEvaluator` | No equivalent |
| `DomainRateLimiter` | `ratelimit/` |
| `MaxRecursiveFanout` (200 sub-jobs) | Grid mode |
| `RecordRepository` (cursor pagination) | PostgreSQL result writer |
| `ArtifactWriter` (CSV/JSON) | `csvwriter`/`jsonwriter` |

The only missing piece is the Google Maps-specific parsing knowledge — and that's ~3,000 lines of Go that need to become Python.

**The architecture is ready. The knowledge needs porting, not the code.**

---

## 4. IMPLEMENTATION PLAN

### Phase 1: Schema Definition (1 hour)

Create `config/schemas/google_maps_business.json`:

```json
{
  "schema_id": "google_maps_business_v1.0",
  "record_type": "business_listing",
  "fields": [
    {"name": "name", "field_type": "string", "required": true, "identity": true},
    {"name": "address", "field_type": "string", "identity": true},
    {"name": "phone", "field_type": "string"},
    {"name": "website", "field_type": "url"},
    {"name": "rating", "field_type": "number"},
    {"name": "reviews_count", "field_type": "number"},
    {"name": "category", "field_type": "string"},
    {"name": "latitude", "field_type": "number"},
    {"name": "longitude", "field_type": "number"},
    {"name": "place_id", "field_type": "string", "identity": true},
    {"name": "opening_hours", "field_type": "string"},
    {"name": "price_level", "field_type": "string"},
    {"name": "plus_code", "field_type": "string"},
    {"name": "description", "field_type": "string"},
    {"name": "photos_count", "field_type": "number"},
    {"name": "claimed", "field_type": "boolean"}
  ],
  "quality_rules": {
    "name": {"min_length": 1},
    "rating": {"range": [1.0, 5.0]},
    "reviews_count": {"min": 0}
  }
}
```

### Phase 2: Google Maps Overlay (2 hours)

Create `config/overlays/google_maps.yaml` referencing the schema, with CSS selectors for Google Maps DOM structure. These selectors come directly from `gmaps/job.go`, `gmaps/multiple.go`, and `gmaps/place.go`:

```
container_selector: "div[role='feed'] > div > div > a"
field_mappings:
  name:              "div.fontHeadlineSmall"
  address:           "div.W4Efsd > span:nth-child(2)"
  rating:            "span[role='img']"
  reviews_count:     "span.UY7F9"
  ...
```

**Critical:** Google Maps renders results dynamically (JavaScript). These selectors are pointers — the actual extraction must happen from Google Maps' internal JSON payload (`ParseSearchResults()` in `gmaps/multiple.go`). The overlay's `container_selector` triggers the "needs JavaScript rendering" pipeline branch.

### Phase 3: JSON-LD + Internal JSON Extraction (3 hours)

Port the core parsing logic from `gmaps/multiple.go` (1,200 lines) into a Python `GoogleMapsJsonParser`:

1. **Search results page:** Google Maps embeds a `window.APP_INITIALIZATION_STATE` or equivalent JSON blob containing business data arrays
2. **Internal JSON structure:** `ParseSearchResults()` extracts from nested arrays at known positions — `business[14]` (arrays of result arrays), `business[7][0]` (individual result object)
3. **Detail page:** `PlaceJob` fetches richer data from a single business URL

This becomes a `GoogleMapsJsonParser` class in `src/extractors/` that:
- Takes the HTML + intercepted JSON payloads from `ScraperEngine`
- Extracts the Google Maps data blob
- Maps the 36 fields to `ExtractedRecord.data` dict
- Computes identity_hash from (place_id, name, address)

### Phase 4: Grid Search Strategy (2 hours)

Port the grid logic from `grid/grid.go` (316 lines) into a `GoogleMapsGridStrategy`:

1. Accept a bounding box (lat1, lng1, lat2, lng2) + search query
2. Subdivide into grid cells sized to keep results per cell under ~120
3. For each cell, construct a Google Maps search URL with the cell's center coordinates and zoom level
4. Emit each cell as a fan-out job through Spacescraper's existing `recursive_fanout` mechanism
5. `MaxRecursiveFanout` (currently 200) caps grid cells per root job

### Phase 5: JavaScript Rendering Strategy (2 hours)

Create `src/extractors/google_maps_strategy.py` as a new `BaseExtractionStrategy`:

```python
class GoogleMapsStrategy(BaseExtractionStrategy):
    """
    Specialized strategy for Google Maps search results.
    
    Requires JavaScript rendering (Playwright) because Google Maps 
    loads business data dynamically. Uses ScraperEngine's intercepted
    JSON payloads for primary extraction, with DOM fallback.
    
    Grid mode: accepts a bounding_box parameter, subdivides into cells,
    and fans out extraction jobs via Spacescraper's recursive fanout.
    """
    
    async def extract(self, html, json_payloads, current_url, overlay, schema):
        # 1. Parse Google Maps internal JSON blob
        parser = GoogleMapsJsonParser()
        businesses = parser.parse_search_results(json_payloads)
        
        # 2. Convert to ExtractedRecord
        records = []
        for b in businesses:
            record = ExtractedRecord(
                record_type="business_listing",
                data=self._map_to_schema_fields(b, schema),
                source_url=current_url,
            )
            record.compute_identity_hash()
            records.append(record)
        
        # 3. Validate against schema
        return self._validate_records(records, schema)
```

### Phase 6: Register in Extraction Pipeline (1 hour)

Add `GoogleMapsStrategy` as a domain-detection branch in `DeterministicExtractionPipeline.extract()`:

```python
async def extract(self, html, json_payloads, current_url, overlay, schema):
    # ... existing overlay/JSON-LD/semantic HTML chain ...
    
    # Google Maps: detected by domain pattern
    if "google.com/maps" in current_url:
        gm_strategy = GoogleMapsStrategy()
        records = await gm_strategy.extract(html, json_payloads, current_url, overlay, schema)
        validated = self._validate_records(records, schema)
        logger.info("GoogleMaps extracted %d businesses", len(validated))
        return validated
    
    # ... fall through to generic pipeline ...
```

### Phase 7: Integration Test (1 hour)

Create `tests/test_google_maps_strategy.py`:
- Test with real Google Maps HTML fixture (saved from a search)
- Test with intercepted JSON payload fixture
- Test grid subdivision math
- Test field mapping accuracy against sample data

---

## 5. WHAT TO KEEP FROM google-maps-scraper

| Artifact | Keep? | How |
|---|---|---|
| `gmaps/entry.go` — 36-field model | **Knowledge only** | Map fields to `ExtractionSchema` definition |
| `gmaps/multiple.go` — JSON parsing | **Port logic** | `GoogleMapsJsonParser` in Python |
| `gmaps/job.go` — scroll+extract | **Port logic** | Use existing `ScraperEngine` + Playwright scroll |
| `gmaps/place.go` — detail page | **Port logic** | New overlay for place detail pages |
| `gmaps/reviews.go` (21KB) | **Port as optional** | Review extraction strategy (defer to Phase 2) |
| `gmaps/emailjob.go` | **Discard** | Spacescraper doesn't do email extraction |
| `grid/grid.go` | **Port logic** | `GoogleMapsGridStrategy` |
| `deduper/` | **Discard** | Spacescraper's `identity_hash` handles dedup |
| `leadsdb/` | **Discard** | External service, not needed |
| Go runner layer | **Discard** | Spacescraper's worker topology replaces it |
| SaaS admin UI | **Discard** | Spacescraper has its own API + metrics |
| Cloud provisioning | **Discard** | Infrastructure is separate concern |

**Total: ~2,000 lines of logic to port, ~28,000 lines discarded.**

---

## 6. OPERATIONAL BENEFITS OF APPROACH B

1. **Unified deployment:** `boot.py` starts API + 3 workers. Adding Google Maps extraction means enabling an overlay — no new process.

2. **Unified rate limiting:** Spacescraper's `DomainRateLimiter` already throttles per-domain. Google Maps gets a dedicated budget (`google.com` → max 10 concurrent, 1 req/s).

3. **Unified observability:** All StrategyObservations (including Google Maps) flow into the same evaluation engine. If JSON-LD extraction outperforms overlay selectors, the pipeline auto-selects through strategy evaluation.

4. **Change detection:** ExtractedRecord's identity_hash detects when a business listing changes (new phone, rating update, closed). The outbox relay emits change events.

5. **Shadow evaluation:** A new Google Maps overlay starts as CANDIDATE, graduates to SHADOW (tested against current ACTIVE), and promotes only when scores prove it's better.

6. **Content-addressed artifacts:** Raw HTML + JSON payloads are stored in the artifact store (`artifacts/{xx}/{yy}/sha256`), enabling offline re-extraction with improved overlays.

---

## 7. RISKS & MITIGATIONS

| Risk | Mitigation |
|---|---|
| Google Maps DOM changes break selectors | Overlay versioning + shadow evaluation detects degradation before promotion |
| IP blocking from Google | Rotating personas via `persona_manager` + proxy support in `ScraperEngine` |
| ~120 result limit per search | Grid mode fans out to sub-cells (already supported via `MaxRecursiveFanout`) |
| Headless Chromium detection | `stealth_brain.py` + persona evasion scripts (WebGL morpher, navigator spoofing) |
| Legal/ToS violation | This is a user decision. Spacescraper is a generic tool — usage policy is separate from architecture |
| Porting errors in JSON parser | Integration tests with real fixture data + schema validation catches mapping gaps |

---

## 8. SUMMARY

**Optimal path:** Create a `GoogleMapsStrategy` as a native extraction strategy in Spacescraper's pipeline, backed by an `ExtractionOverlay` + `ExtractionSchema`, using the existing Playwright/ScraperEngine infrastructure.

**Effort:** ~12 hours for core extraction (schema, overlay, JSON parser, grid, strategy). +4 hours for reviews and polish.

**Key insight:** google-maps-scraper is 90% infrastructure (runner layer, web UI, cloud provisioning, SaaS admin, job queue, telemetry) and 10% extraction knowledge (JSON parsing, DOM selectors, grid math). Spacescraper already has the 90% — it only needs the 10%.

**Architectural win:** This integration turns Google Maps scraping from a standalone Go binary into a configurable, evaluatable, version-controlled extraction strategy that benefits from all of Spacescraper's infrastructure: state machines, outbox events, content-addressed storage, rate limiting, AI enrichment, and strategy auto-selection.
