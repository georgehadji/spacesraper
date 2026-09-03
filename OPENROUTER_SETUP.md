# OpenRouter Integration

The AI layer routes each job to its own model. Every model id, endpoint,
timeout, temperature and prompt budget lives in one file:

**`src/infrastructure/ai/ssot.py`** — the single source of truth.

Nothing else in `src/` may hardcode any of those. `tests/test_ai_ssot.py`
enforces that by scanning the tree, so a stray literal fails CI.

## Enabling

```bash
export AI_PROVIDER=openrouter
export AI_OPENROUTER_API_KEY=sk-or-...
export AI_GEMINI_API_KEY=...        # optional but recommended, see Embeddings
```

No model id is needed — the pins are in the SSOT.

## Per-job models

Chosen from the live catalogue by value-for-money, defined as
`artificial_analysis.intelligence_index / blended $ per 1M`, where blended is
`0.75 × prompt + 0.25 × completion` (input-weighted, because every job sends a
large prompt and gets a small JSON object back).

| Job | Model | IQ | $/1M | Reasoning | Temp |
|---|---|---|---|---|---|
| `overlay` | `z-ai/glm-5.3-flash` | 57.5 | 0.119 | on, `low` effort | 0.0 |
| `enrich` | `deepseek/deepseek-v4-flash-0731` | 51.8 | 0.094 | **off** | 0.2 |
| `heal` | `inclusionai/ling-3.0-flash` | 37.8 | 0.032 | **off** | 0.0 |
| `generate` | `deepseek/deepseek-v4-flash-0731` | 51.8 | 0.094 | **off** | 0.7 |
| `embed` | `text-embedding-004` (Gemini) | — | — | — | — |

Reproduce the ranking at any time:

```bash
python scripts/refresh_openrouter_models.py --rank
```

### Why these

- **overlay** is the correctness-critical job — a wrong selector map silently
  corrupts every record from that site — and its output is cached per page, so
  volume is low and capability matters more than price. GLM 5.3 Flash is the
  highest-IQ model in the sub-$0.15 tier, within 8 points of the catalogue
  frontier at 1/168th the price.
- **enrich** is the highest-volume job (one call per record), so value score
  dominates. DeepSeek V4 Flash has the best score of anything above IQ 50.
- **heal** runs on the failure path and is latency-sensitive; Ling 3.0 Flash
  has the best value score in the entire catalogue.

## Fallbacks

Each job has a fallback chain sent as OpenRouter's `models` array. OpenRouter
tries them in order on context-length errors, moderation blocks, rate limits
and provider downtime, and bills whichever model answers. The adapter logs a
warning when the response's `model` field is not the primary.

| Job | Chain |
|---|---|
| `overlay` | glm-5.3-flash → deepseek-v4-flash-0731 → minimax-m3 |
| `enrich` | deepseek-v4-flash-0731 → solar-pro4 → glm-5.3-flash |
| `heal` | ling-3.0-flash → solar-pro4 → mimo-v2.5 |

Selection rules, enforced by tests: a fallback must be a **different vendor**
from the primary (or it does not survive the outage it exists for), must
**accept `temperature`** (one payload serves the whole chain), and must be
within 25% of the primary's IQ.

## Temperature

**81 of 424 catalogue models reject `temperature` outright** — 59 OpenAI,
12 Anthropic, 4 Google. OpenAI reasoning models are fixed at 1. The adapter
emits the parameter only when every model in the chain accepts it.

Values are task-shape decisions: overlay, heal and enrich each have one correct
answer, so sampling entropy can only hurt. Only `generate` keeps 0.7.

## Reasoning

**Reasoning tokens bill as output tokens**, and 109/424 models reason by
default (97 mandatorily). Leaving the vendor default in place is a cost
decision made by the vendor, so every job states what it wants:

- `overlay` — reasoning **on** at `low` effort. Its model makes reasoning
  mandatory and defaults to `max` (~95% allocation), which is why the timeout
  is 45s rather than 10s.
- everything else — `{"enabled": false}`.

The trace is always requested with `exclude: true`; nothing here reads it.

## Prompt caching

Both primaries cache automatically with no `cache_control` breakpoints:
DeepSeek reads cached input at **0.1x**, Z.AI at **~0.2x**.

This is contingent on prompt layout. Providers cache on a *prefix match*, so
every prompt builder puts fixed instructions and the output schema **first**
and the variable HTML or record fields **last**. Reordering a prompt to
interpolate variable data near the top silently stops the cache matching —
nothing breaks, no test fails, the bill just goes up. See
`ssot.PROMPT_CACHE_NOTE`.

## Embeddings

**OpenRouter serves no embedding models.** All 424 catalogue entries are
chat-completion models and there is no embeddings route, so embeddings stay on
Gemini regardless of `AI_PROVIDER`.

When `AI_PROVIDER=openrouter`, the factory injects the Gemini adapter as an
embedder if `AI_GEMINI_API_KEY` is set. Without it, embeddings return `None`
and deduplication falls back to fuzzy title matching — degraded, not broken.

## Discovery web search

OpenRouter can also back Discovery's query-to-URL search, alongside the
existing `noop` / `duckduckgo` / `serper` options:

```bash
export DISCOVERY_SEARCH_PROVIDER=openrouter
export DISCOVERY_SEARCH_API_KEY=sk-or-...   # falls back to AI_OPENROUTER_API_KEY
```

Uses the current server-tool syntax:

```json
"tools": [{"type": "openrouter:web_search", "parameters": {"max_results": 5}}]
```

The older `plugins: [{"id": "web"}]` form and the `:online` model suffix are
deprecated and are not used — a test asserts neither appears in the source.

### Cost

**Unlike the other search adapters, this one bills per request** — $0.007 on
the Exa instant/fast/auto tier, on top of tokens (Parallel $0.001–0.005,
Perplexity $0.005). The SSOT records the conservative figure as
`WEB_SEARCH_PRICE_PER_REQUEST_USD`.

Two guards follow from that:

- `max_results` is clamped **before** the request, to
  `min(requested, DISCOVERY_MAX_FANOUT, WEB_SEARCH_MAX_RESULTS_CAP)`. Discovery
  caps how many hits can become jobs, so asking a billed API for more than that
  is spend that cannot change the outcome.
- Results are cached, so a repeated identical query does not re-bill. An open
  circuit breaker also stops billing entirely.

### Trust

The other adapters read a real SERP. This one reads a model's response, and a
model can invent a plausible URL. The adapter parses **only `url_citation`
annotations** — never URLs from the reply text — because annotations record
pages the tool actually fetched, whereas prose URLs are generated tokens.
Reading them would let a model inject arbitrary targets into the crawl queue.

The adapter does **not** enforce the domain allowlist itself and cannot bypass
it: `DiscoveryService.discover()` applies `UrlPolicy`, the SSRF guard and the
fan-out budget to every hit, and refuses outright to run against an empty
allowlist. Discovery also stays dark by default (`DISCOVERY_ENABLED=false`).

## Maintaining the catalogue

```bash
python scripts/refresh_openrouter_models.py --diff     # fetch, show changes, save
python scripts/refresh_openrouter_models.py --dry-run  # show changes, save nothing
python scripts/refresh_openrouter_models.py --rank     # reproduce the ranking
python scripts/refresh_openrouter_models.py --verify   # check pins still resolve
```

`--verify` exits non-zero when a pinned model has left the catalogue, and
annotates pins whose reasoning or temperature behaviour differs from what the
SSOT records. Worth running in CI.

The snapshot at `src/infrastructure/ai/models.json` is generated — do not
hand-edit. It stores only fields the API actually publishes; anything
unpublished is `null` rather than guessed, because these values are the
evidence behind the pins.

## Overrides

Repin one job without editing the SSOT:

```bash
export AI_MODEL_ENRICH=some-vendor/some-model
export AI_TEMPERATURE_UNSUPPORTED=1   # if that model rejects temperature
```

An override drops the fallback chain and the recorded price/benchmark, since
none of those were measured against the substituted model.

`AI_OPENROUTER_MODEL` forces *every* job onto one model. It disables per-job
routing, fallbacks and reasoning control — an escape hatch, not a normal
configuration.

## Tunables

| Variable | Default |
|---|---|
| `AI_MAX_CONCURRENCY` | 10 |
| `AI_RESULT_CACHE_SIZE` | 500 |
| `AI_EMBEDDING_CACHE_SIZE` | 500 |

Retry counts, breaker thresholds and cooldowns are in `ssot.RESILIENCE`.
