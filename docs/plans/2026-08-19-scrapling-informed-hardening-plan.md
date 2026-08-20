# Scrapling-Informed Hardening Plan

**Date:** 2026-08-19
**Status:** Draft for review
**Branch at time of writing:** `fix/e2e-correctness-and-headless-cli` @ `ed6f9b5`
**Relationship to other plans:** **Amendment.** This plan does not replace
`2026-08-13-capability-enhancement-plan.md` (P0–P6) or
`2026-08-10-architecture-remediation-to-8.5.md` (W-series). It amends them.

---

## Table of Contents

- [0. Scope and Relationship to Existing Plans](#0-scope-and-relationship-to-existing-plans)
- [1. Source of Findings](#1-source-of-findings)
- [2. Design Principles (Inherited + Added)](#2-design-principles-inherited--added)
- [3. Dependency Decision](#3-dependency-decision)
- [4. Phase S — Stabilization (pre-P0, independently shippable)](#4-phase-s--stabilization-prep0-independently-shippable)
- [5. Amendments to Existing Phases](#5-amendments-to-existing-phases)
- [6. Phase P7 — Extraction Substrate](#6-phase-p7--extraction-substrate)
- [7. Phase P8 — Agent Surface and Export](#7-phase-p8--agent-surface-and-export)
- [8. Security and Safety](#8-security-and-safety)
- [9. Testing Strategy](#9-testing-strategy)
- [10. Sequencing](#10-sequencing)
- [11. Risk Register (additions to R1–R8)](#11-risk-register-additions-to-r1r8)
- [12. Non-Goals](#12-non-goals)

---

## 0. Scope and Relationship to Existing Plans

The 08-13 capability plan surveyed the ecosystem (including Scrapling) and
produced phases P0–P6. A subsequent close read of Scrapling's source —
parser, engines, spiders, AI layer — surfaced material that plan does not
contain. This document carries **only that delta**.

### What this plan explicitly does NOT re-litigate

Already correctly specified in 08-13; no changes proposed:

| Already planned | Where |
|---|---|
| Link discovery, frontier, robots.txt, sitemaps, pagination | P2 |
| HTTP-first tiering with `curl_cffi` TLS impersonation | P1 |
| Adaptive rendering decision backed by `DomainProfile` | P1 |
| `smart_crawler` Valkey client injection | P1 |
| Session pool with health scoring, proxy wiring, `stealth_brain` read-back | P3 |
| Selector self-healing via element signatures, repair ladder | P4 |
| API verb taxonomy, change tracking | P5 |
| Scheduling, autoscaling, graceful drain, real SLO telemetry | P6 |
| P0 as a hard entry gate for everything | §5 |

If a topic above appears below, it is because this plan **changes the
specification**, not because it is being proposed fresh. Such items are
labelled `AMENDS P<n>`.

### What this plan adds

1. **Phase S** — six defects that make the current system worse than doing
   nothing, all fixable without architectural change, all landable before P0.
2. **Amendments** to P1, P3, P4 where Scrapling's implementation reveals the
   08-13 specification is underspecified or would produce a detectable result.
3. **P7 Extraction Substrate** — parser replacement and content-addressed
   selection. 08-13 has no equivalent; its P4 assumes the current parser.
4. **P8 Agent Surface** — MCP server and export primitives. 08-13's P5 covers
   HTTP verbs only.
5. **§8 Security** — including two new findings and the transport-agnostic
   SSRF design that R2 calls for but does not specify.

---

## 1. Source of Findings

Every claim below was read from source in the working tree, not inferred.
File:line references are current as of `ed6f9b5`.

Findings fall into three buckets:

- **Verified defects** — read directly from the named file and line.
- **Verified absences** — whole-tree grep excluding `venv/`, stated as absence.
- **Ported techniques** — read from Scrapling 0.4.14 source, restated as a
  design for this codebase (not a copy-paste instruction).

Where the 08-13 plan already recorded a finding, this plan cites it rather
than re-deriving it.

---

## 2. Design Principles (Inherited + Added)

### Inherited from 08-13 §3 — unchanged, governing

1. New capability = new Port + adapter, never a new root-level module.
2. Chain of Responsibility for escalation ladders.
3. Policy objects for decisions, separated from mechanisms.
4. Learn via the existing observation loop.
5. Functional core, imperative shell. Pure logic in `src/domain/`, frozen models.
6. One capability, one implementation.
7. Politeness fails closed; budget caps fail open.

### Added by this plan

**8. A Port needs two adapters or a test seam. Otherwise it is a function.**

Principle 1 is a real constraint and it is also the most common source of
speculative abstraction. The discriminator: introduce a `Protocol` only when
there are genuinely two implementations (`FetcherPort`: http + browser) or when
the seam exists to substitute I/O in tests (`RobotsPort`). Do not introduce
`SelectorPort`, `MarkdownPort`, `FingerprintPort` — each would have exactly one
implementation and no test value, because the underlying operation is pure and
already testable. Those are module-level functions in `src/domain/`.

This principle exists because the inventory found thirteen zero-caller modules.
The cheapest way to not add a fourteenth is to not create the interface.

**9. Fingerprint attributes are a set, never a list.**

Any fingerprint attribute that can contradict another must be constructed
together and validated together. A User-Agent, `navigator.platform`,
`Sec-CH-UA-Platform`, the WebGL renderer, the screen dimensions, and the
timezone are one value object with an invariant, not six independent knobs.
The current code treats them as independent, which is the root cause of every
defect in S1. Enforce with a single constructor and a validator; never allow
partial application.

**10. Untrusted page content may not become executable configuration without
validation.**

Scraped HTML is attacker-controlled. Anything derived from it that the system
later *executes* — a CSS selector, an XPath, a URL to fetch, a JSON schema —
crosses a trust boundary and gets validated at that boundary. This applies
most sharply to LLM-generated overlays, which are page-content-derived and are
executed by the extraction pipeline.

**11. Deliberate corner-cuts carry a named ceiling.**

Where a simple implementation has a known limit (in-memory cache, single-host
scheduler, O(n²) similarity scan), the code carries a comment naming the
ceiling and the upgrade path. Silent limits become 3am pages.

---

## 3. Dependency Decision

An earlier draft of this analysis recommended adopting Scrapling wholesale as
the fetch layer. **That recommendation is withdrawn.** It was uncosted and it
conflicts with 08-13 P1.

`scrapling[fetchers]` pulls **both** `playwright` and `patchright` — two
browser drivers and two Chromium installs — plus `curl_cffi`, `browserforge`,
`markdownify`, `protego`, `w3lib`, `tld`, `orjson`. In a repo with a Dockerfile
and docker-compose, that roughly doubles image size for capability that
overlaps what P1 and P3 already plan to build.

### Decision

| Need | Take | Rationale |
|---|---|---|
| TLS impersonation (P1 Tier 1) | `curl_cffi` **direct** | Already the named P1 dependency. No change. |
| Browser stealth (P1 Tier 2, P3) | **Port techniques**, keep Playwright | Techniques in A1 are ~150 lines. A second driver is not worth it. |
| HTML parsing + adaptive relocation (P7) | `scrapling` **base only** | No `[fetchers]` extra → no browsers. Deps: lxml, cssselect, orjson, tld, w3lib, babel. BSD-3. |
| robots.txt (P2) | `protego` **direct** | Already implied by P2. |
| Link canonicalization (P2) | `w3lib` **direct** | Transitive via scrapling base anyway. |

**Net new top-level dependencies: `curl_cffi`, `scrapling`, `protego`,
`w3lib`.** No new browser driver. No image-size regression beyond lxml.

### Prerequisite: repository licensing

There is **no `LICENSE` file in the repository root**. Adding BSD-3 and
BSD-licensed dependencies is unproblematic, but the project's own license
posture should be stated before the dependency surface grows. This is a
one-file task and it blocks nothing technically — but it should not stay open.

---

## 4. Phase S — Stabilization (pre-P0, independently shippable)

**Goal:** remove behaviour that is worse than its own absence.

Phase S is deliberately outside the P0 gate. Every item is a bounded bug fix
in an existing module with no new Port, no new dependency, and no change to
the live-path wiring that P0 is repairing. They can land in any order, in
parallel with P0, without touching P0's files.

**Entry criteria:** none.
**Exit criteria:** all S-items green, no new module with zero callers.

---

### S1 — Fingerprint coherence (`CRITICAL`)

The stealth stack contradicts itself. Each contradiction is independently
detectable, and their conjunction is a stronger signal than plain Playwright
defaults would be. Verified:

**All claims below were reproduced in a live Chromium via Playwright 1.61**
during an adversarial verification pass. Several earlier-drafted claims were
refuted by that pass and have been removed or restated; see Appendix A.

**The root defect is not a fingerprint mismatch — it is that the evasion script
throws.**

`Object.defineProperty` defaults to `configurable: false`. The context-level
script at `pool.py:99` therefore creates `navigator.webdriver` as a
**non-configurable** own property. The page-level script at `engine.py:66`
then attempts to redefine it and throws:

```
TypeError: Cannot redefine property: webdriver
```

An init script that throws aborts at the throw site. Everything after
`engine.py:66` in that script **never executes**. Reproduced against the
production ordering (context script, then page script, then evaluate):

```
navigator.webdriver   -> undefined
pageerror             -> "Cannot redefine property: webdriver"
uaOverrideApplied     -> false      # engine.py:67  never runs
webglMorpherApplied   -> false      # engine.py:69-74 never runs
```

So the stack does not do what it appears to do. The consequences:

| Defect | Evidence | Status |
|---|---|---|
| Evasion script dies on line 1 | `pool.py:99` non-configurable define; `engine.py:66` redefine throws. Reproduced. | Every browser page load raises a JS error, itself observable. |
| `navigator.userAgent` override never applies | `engine.py:67` is after the throw. | `navigator.userAgent` reports the **pool** UA (`pool.py:94`, Windows Chrome/120), not the persona UA. |
| WebGL morpher never applies | `engine.py:69-74` is after the throw. | Dead code twice over — see next row. |
| No WebGL at all | `pool.py:58-59` `--disable-gpu` **plus** `--disable-software-rasterizer`. Reproduced: `getContext('webgl')` and `getContext('webgl2')` **both return null**. Neither flag alone does this. | This is the real signal. WAFs check whether WebGL *exists*; a browser with none is anomalous. |
| Genuine UA incoherence, via a different route than first thought | `engine.py:61` `set_extra_http_headers` **does** apply (it is a Playwright API, not the init script), so the HTTP header carries the persona UA. `navigator.userAgent` carries the pool UA. `Sec-CH-UA*` derive from the launched Chromium. | Three surfaces, three answers. |
| Cross-OS incoherence | `persona.py:18,31,54-61` randomises OS across Windows/macOS/Linux for the header UA, while `Sec-CH-UA-Platform` stays pinned to the real platform. | Header claims macOS, client hints say otherwise. |
| Renderer/OS incoherence | `persona.py:46-47` can pair `"Apple Inc."` vendor with an NVIDIA D3D11 renderer under a Windows UA. | Latent — unreachable while WebGL is off. |
| WebGL2 never covered | `engine.py:69` patches `WebGLRenderingContext` only; `WebGL2RenderingContext` appears nowhere in the repo. | Latent, becomes live the moment WebGL is re-enabled. |
| Naive patches | `pool.py:101` `plugins => [1,2,3,4,5]` — a plain Array of Numbers, not a `PluginArray`; `instanceof`, `toString.call`, `.item(0)`, `[0].name` all diverge. `pool.py:100` `window.chrome = {runtime:{}}`. | Live (these are in the context script, before the throw). |
| Playwright's own feature defaults discarded | `pool.py:63-64` passes `--disable-features` twice. Playwright *prepends* its own `--disable-features=<17 defaults>` and appends caller args **last**, so `IsolateOrigins` (`:64`) is the only one honoured — discarding `TranslateUI` **and** Playwright's whole default set (Translate, HttpsUpgrades, PaintHolding, MediaRouter, ThirdParty…). | Live. Larger than a lost translate popup. |
| Unconsumed persona fields | `persona.py:48-49` `canvas_bitmask`, `audio_pinnacle` read nowhere. | Safe to delete — they are the *last* RNG draws. |
| `stealth_brain` write-only | `get_best_attributes` (`stealth_brain.py:61`) has exactly one repo-wide hit: its own definition. Written at `worker_scraper.py:268`, never read. | Already recorded in 08-13 P3. |

**Two claims from the earlier draft were refuted and must not be published:**

- *"Impossible viewport/screen pair."* **False.** Playwright's
  `set_viewport_size` sets `screen` to the viewport
  (`coreBundle.js:21764` — `_setEmulatedSize({viewport, screen})`). No
  anomaly exists.
- *"Four dead persona fields."* **Overstated.** `has_touch` is a hardcoded
  `False`, identical to Playwright's default — wiring it is a no-op. And
  `device_scale_factor` (`persona.py:41`), while unconsumed *as a value*,
  draws from the shared seeded `random.Random` **before** the `webgl_vendor`
  and `webgl_renderer` draws (`:46-47`). Deleting it shifts the RNG stream and
  silently changes the WebGL fingerprint every `persona_id` maps to —
  reproduced: seed `"example.com"` yields vendor `NVIDIA` with the field
  present, `Apple` without it. Since StealthBrain persists scores keyed
  `f"{ua}|{renderer}"` (`stealth_brain.py:55-56`), removing it invalidates all
  historical scores and breaks the determinism contract documented at
  `persona.py:26`. **Keep the draw, or migrate the scores deliberately.**

**Design — Value Object with an enforced invariant (Principle 9).**

```
src/domain/fingerprint.py          (pure, mypy-strict, zero I/O)
    @dataclass(frozen=True)
    class Fingerprint:
        user_agent, platform, ua_platform, vendor, renderer,
        viewport, screen, device_scale_factor, has_touch,
        hardware_concurrency, device_memory, locale, timezone

    def build_fingerprint(chromium_major: int, profile: OsProfile,
                          rng: random.Random) -> Fingerprint
    def validate_fingerprint(fp: Fingerprint) -> list[str]   # [] == coherent
```

Rules the builder enforces, and the validator asserts:

- `user_agent` major version **equals the driven Chromium major**. Read it
  once at startup from the installed Playwright's `browsers.json` (Scrapling's
  `driven_browser_version()` technique) and cache it. This single change
  removes the largest tell in the stack.
- OS profile determines `platform`, `ua_platform`, the UA OS token, the
  plausible renderer set, and the timezone/locale pool — together or not at all.
  Because `Sec-CH-UA-Platform` is derived by Chromium and cannot be set per
  page, the OS profile must be pinned to the **host platform**, not randomised.
  Randomising it guarantees a header/client-hint contradiction.
- Renderer is drawn only from the vendor's own set for that OS.
- The RNG draw order is part of the contract. `persona_id` is documented as
  deterministic (`persona.py:26`) and StealthBrain scores are keyed on the
  resulting `ua|renderer` pair, so changing the number or order of draws is a
  data migration, not a refactor. Either preserve the draw sequence or version
  the persona schema and migrate the score set explicitly.

**Mechanism changes in `pool.py` / `engine.py`:**

- Fingerprint is applied at **context creation**, not per page. `user_agent`,
  `viewport`, `screen`, `locale`, `timezone_id`, `device_scale_factor`,
  `is_mobile`, `has_touch` are `new_context()` options; Playwright then derives
  consistent client hints. Delete `engine.py:61` (`set_extra_http_headers`)
  and `engine.py:67` (the `navigator.userAgent` override) — both become
  unnecessary and both are the source of the mismatch.
- Delete **both** `webdriver` overrides. Playwright's own
  `--disable-blink-features=AutomationControlled` handles this; a JS
  `defineProperty` on `navigator` is itself a signal.
- Delete `plugins`, `deviceMemory`, `hardwareConcurrency`, `window.chrome`
  init scripts. Move the last two to `new_context()`-adjacent real options
  where Playwright supports them; drop the rest rather than fake them badly.
- Re-enable WebGL: remove `--disable-gpu` and `--disable-software-rasterizer`.
  Use `--use-gl=swiftshader` for headless software GL. Keep the renderer
  override only if it is applied to `WebGL2RenderingContext` as well.
- Merge the two `--disable-features` values into one comma-separated flag.
- Remove `bypass_csp=True` (`pool.py:93`) — see §8.
- `canvas_bitmask` and `audio_pinnacle` (`persona.py:48-49`) are the final RNG
  draws and are read nowhere — delete them, or inject them. Deleting them is
  safe precisely because nothing is drawn after them. Do **not** delete
  `device_scale_factor` without a score migration (see above).
- **Verify the init script does not throw.** Whatever survives of the evasion
  script must be checked with a `pageerror` listener in a contract test. A
  silently-throwing init script is how this defect went unnoticed.

**Interaction with P3.** P3 introduces `SessionPool` with persona-proxy
binding and will own fingerprint lifecycle. S1 does not build that. S1 makes
the *current* fingerprint coherent so P3 inherits a correct value object
instead of re-deriving one. `Fingerprint` and `build_fingerprint` are exactly
what P3's `PersonaFactory` will consume.

**Deliverables**
- [ ] `src/domain/fingerprint.py` — frozen value object, builder, validator; mypy-strict
- [ ] `pool.py` applies fingerprint at context creation; init-script block reduced to what cannot be a context option
- [ ] `engine.py` per-page UA/viewport patching deleted
- [ ] Chromium version read once from the driver, cached
- [ ] `persona.py` reduced to an `OsProfile` table consumed by the builder, or deleted if the builder subsumes it
- [ ] Unit test: `validate_fingerprint` returns `[]` for 1000 seeded builds and non-empty for each hand-built incoherent case
- [ ] Contract test: a launched context's `navigator.userAgent`, `navigator.platform`, `Sec-CH-UA-Platform`, `screen.width` and the HTTP `User-Agent` header all agree

---

### S2 — Bot self-identification on the HTTP path (`CRITICAL`)

`http_client.py:38` sends
`User-Agent: Spacescraper/2.4 (Enterprise Pipeline; <author full name>)` on
every outbound request, over a stock httpx TLS fingerprint. Turbo-mode
fetches (`worker_scraper.py:347`), cache validation, and webhook delivery all
share it. Two distinct problems: it is a self-declaring bot signature, and it
transmits a real person's name to every host contacted.

**Design — split by trust direction, not by protocol.**

The current singleton conflates two populations with opposite requirements:

| Client | Targets | Requirements |
|---|---|---|
| `internal_http` (keep httpx + `SSRFValidatingTransport`) | webhooks, notifications, LLM API | SSRF validation, honest neutral UA, no impersonation |
| `target_http` (P1 Tier 1, `curl_cffi`) | scrape targets | TLS impersonation, fingerprint coherence, SSRF validation |

S2 does the split and fixes the UA. It does **not** build the curl_cffi tier —
that is P1. Until P1 lands, `target_http` is httpx with a neutral,
fingerprint-consistent UA drawn from `build_fingerprint` (S1), so the two paths
already agree.

**Deliverables**
- [ ] Personal name removed from all UA strings; repo-wide grep clean
- [ ] Two named clients; call sites updated; `SSRFValidatingTransport` on both
- [ ] Target UA sourced from `Fingerprint`, not a literal
- [ ] Test: no outbound UA contains `Spacescraper` or a personal name

---

### S3 — Turbo-mode promotion learns the wrong URL (`CRITICAL`)

`worker_scraper.py:244-247` promotes a domain to hybrid/turbo when the browser
run captured any JSON XHR. `_perform_turbo_scrape` (`:347`) then re-fetches
**`job.url`** — the page — and only retains a body whose `content-type`
contains `json`. The page returns HTML, so turbo yields nothing.

The failure mode is worse than a no-op. Each empty yield is recorded as a hard
`JobState.FAILED` with no in-job browser fallback (`:193-210`), and
`TURBO_MISS_THRESHOLD = 3` empty yields are needed to demote. Every domain
whose pages make XHR calls — most of them — costs three failed jobs to learn
nothing.

Compounding it: `engine.py:110` only matches `application/json` exactly,
missing `application/ld+json`, `text/json`, `application/vnd.api+json`; and
`engine.py:118-120` swallows every interception error, so a malformed response
is indistinguishable from a site with no API.

**Design — record the endpoint, not the page (Scrapling's `capture_xhr`).**

- `engine.py` records, per intercepted response, the **URL, status, and
  content-type** of the endpoint. Content-type match widens to a
  `*/json`-suffix test.
- Promotion stores those **endpoint URLs** against the domain (on
  `DomainProfile`, reusing the existing observation substrate per Principle 4)
  — never the page URL.
- Turbo replays endpoints. Empty or non-2xx result **demotes and falls through
  to the browser inside the same job**, so a wrong guess costs latency, not a
  failed job.
- Interception errors are counted and logged at debug with the exception, not
  swallowed (Principle: they are signal, and they feed this decision).

**Deliverables**
- [ ] Endpoint capture with widened content-type match and a size cap (see §8)
- [ ] Promotion keyed on endpoint URL; page URL never promoted
- [ ] Empty turbo → in-job browser fallback; no `JobState.FAILED` for an empty yield
- [ ] `turbo_endpoint_hit` / `turbo_endpoint_miss` metrics
- [ ] Regression test: fixture where page URL returns HTML and endpoint returns JSON — asserts one job, one success, endpoint promoted

---

### S4 — Unconditional `networkidle` (`HIGH`)

`engine.py:153` navigates with `wait_until="networkidle"` and a 35s timeout on
every fetch. Any page with polling, ads, analytics beacons, or a websocket
never reaches network idle, so every such fetch burns the full timeout.

**Design.** Default to `wait_until="load"`. Make `network_idle` a per-job
field on `ScrapeJob` (default `False`). When requested, wrap it so a timeout
is a shrug rather than a failure — the page is usually usable. Add
`wait_selector` as the precise alternative, since a caller who knows the
selector never needs idle at all.

**Deliverables**
- [ ] `load` default; `network_idle` and `wait_selector` per-job fields
- [ ] Idle wait wrapped, timeout non-fatal, logged at debug
- [ ] Test: a fixture page with a never-settling request returns within the
      load budget rather than at timeout

---

### S5 — Prompt injection into executable configuration (`HIGH`)

`compact_html_for_prompt` reduces tokens; it is not a safety pass. Hidden
elements, `aria-hidden` subtrees, `<template>` bodies and zero-width
characters survive it, and its attribute allowlist keeps `class`, `id` and
`href` — precisely what selector-bait would use.

The output of that prompt is parsed as JSON and used as **extraction
configuration**: `container_selector` and `field_mappings` that the pipeline
then executes. That is a path from attacker-controlled page content to
attacker-influenced extraction behaviour (Principle 10).

**Design — two independent gates.**

*Gate 1 — sanitize input.* A pure function in `src/domain/`, ahead of
compaction: drop CSS-hidden / `aria-hidden` / `<template>` subtrees via one
precompiled XPath; strip zero-width (`U+200B–200D`, `FEFF`, `2060`, `180E`)
and C0 control characters from text and tails; drop comments.

*Gate 2 — validate output.* An LLM-returned overlay is untrusted config until
it passes: every selector parses as valid CSS; every selector resolves against
the sampled HTML; the field set matches the requested schema; no selector
reaches outside the container. Only then may it be persisted — and only as
`CANDIDATE`, never `ACTIVE`. Promotion stays behind the existing shadow
evaluator and human gate (08-13 R6 already requires this; S5 makes the
*validation* explicit, which R6 does not).

**Deliverables**
- [ ] `sanitize_for_llm(root) -> root` in `src/domain/`, pure, mypy-strict
- [ ] Called on every path that sends page content to a model — `generate_overlay`, `heal_selector`, enrichment
- [ ] `validate_overlay(overlay, sample_html, schema) -> list[str]` gate before persistence
- [ ] Test: page with a hidden `<div>` carrying instruction text produces identical model input to the page without it
- [ ] Test: overlay with an unparseable or non-resolving selector is rejected, not persisted

---

### S6 — Silent failure and suppressed lint (`MEDIUM`)

`stealth_brain.py:71` bare `except:`; `engine.py:118-120` and `:207`,
`pool.py:151, 164, 238, 246, 251` blanket `except Exception: pass`.

Note that `pyproject.toml` currently **ignores `E722` (bare-except)** in ruff's
config, so the linter cannot surface these. Fixing the code without removing
the suppression leaves the door open.

**Design.** Narrow each to the exception actually expected; log at debug with
`exc_info`; count the ones that carry signal (interception failures feed S3).
Then remove `E722` from the ignore list so regressions fail CI.

**Deliverables**
- [ ] Named exception types + debug logging at each site
- [ ] `E722` removed from `[tool.ruff.lint] ignore`; CI green
- [ ] Interception-failure counter wired to observability

---

### S7 — The entire AI path fails on every real call (`CRITICAL`)

Found during verification, not in the original study.

`src/infrastructure/ai/client.py:109`:

```python
data = await response.json()
```

`httpx.Response.json()` is **synchronous**. It returns a `dict`. Awaiting a
`dict` raises `TypeError: object dict can't be used in 'await' expression`,
which is caught by the `except Exception` at `:112` — so every call retries
three times with exponential backoff, then calls `_record_failure` and returns
`None`.

Consequences: `generate_overlay`, `heal_selector`, `enrich_opportunity`, and
`compute_embedding` return `None` on every invocation against a real endpoint.
After five such failures the circuit breaker opens for 300s. **No AI feature in
this system has ever worked against the live Gemini API.**

This is invisible in CI because the test supplies a fake response object with
an `async def json(self)` — `tests/test_ai_cache_and_client.py:203-204` — so
the mock satisfies the `await` that the real client cannot.

This finding interacts with S6 directly: the `except Exception` that hides it
is exactly the pattern S6 removes. It also silently invalidates any assumption
elsewhere that AI-generated overlays exist — including parts of 08-13's P4,
which builds a repair ladder on top of `heal_selector`.

**Design.** Drop the `await`. Then fix the test double so it cannot mask the
same class of bug again: the fake must mirror `httpx.Response`'s real sync/async
split, or better, use `httpx.MockTransport` against the actual `httpx.Response`
type rather than a hand-rolled stand-in.

**Deliverables**
- [ ] `await` removed; verified against a real `httpx.Response`
- [ ] Test doubles replaced with `httpx.MockTransport` so the response contract is the real one
- [ ] Integration test that fails if `_call_gemini_api` returns `None` for a well-formed 200
- [ ] Audit every other `await …json()` / `await …text()` in the repo for the same error

---

## 5. Amendments to Existing Phases

### A1 `AMENDS P1` — Stealth techniques for Tier 2

P1 specifies Tier 2 as "existing `BrowserContextPool` + persona (Playwright)".
With S1 landed, the fingerprint is coherent — but the launch profile is still
Playwright-default, which is itself identifiable. Four additions, none
requiring patchright:

1. **Strip Playwright's own tells.** Launch with `ignore_default_args` for
   `--enable-automation`, `--disable-extensions`, `--disable-default-apps`,
   `--disable-component-update`. Playwright adds these; they are read directly.
2. **Adopt the flag set, not the anti-set.** Replace ad-hoc flags with a
   curated list. Notably `--start-maximized` (headless-check bypass),
   `--disable-blink-features=AutomationControlled`, and a `--blink-settings`
   line forcing desktop hover/pointer types — headless Chromium otherwise
   reports touch-style pointer capabilities.
3. **Prefer flags over JS for fingerprint noise.**
   `--fingerprinting-canvas-image-data-noise` for canvas,
   `--webrtc-ip-handling-policy=disable_non_proxied_udp` +
   `--force-webrtc-ip-handling-policy` for WebRTC leak under proxy (needed by
   P3), `--dns-over-https-templates=...` for DNS leak. A Chromium flag has no
   JS-observable patch surface; a `defineProperty` override does.
4. **Context options that matter.** `color_scheme: "dark"` (defeats the
   `prefersLightColor` heuristic), real `screen` alongside `viewport` (S1),
   `ignore_https_errors` for MITM-proxy compatibility.

**Also amends P1's `BlockSignalDetector`.** P1 says "generalizes the existing
four-title-string check". Specify it: status ∈ {403, 429, 503}, challenge
markers (`cType: 'managed'|'interactive'|'non-interactive'`, turnstile script
tag), content-length collapse relative to the domain's rolling median, and the
existing title strings. Detection only — solving stays a non-goal per 08-13 §6.

**Deliverables**
- [ ] Curated launch profile + `ignore_default_args`; flags deduplicated (no repeated `--disable-features`)
- [ ] Flag-based canvas/WebRTC/DNS controls replacing JS equivalents
- [ ] `BlockSignalDetector` as a pure function in `src/domain/`, shared by both tiers
- [ ] Test: detector fires on each fixture class; no false positive on a clean 200

---

### A2 `AMENDS P1/P3` — Proxy rotation requires context isolation

P3 says "Playwright context `proxy=`". Underspecified in a way that matters:
**a context reused across proxies carries one cookie jar, one localStorage,
and one persistent identity across multiple IPs.** That correlation is a
stronger signal than not rotating at all.

**Design.** Rotating the proxy requires a fresh `BrowserContext` bound to that
proxy, discarded on release. This conflicts with `BrowserContextPool`'s current
model of long-lived warm contexts, so P3's `SessionPool` must own the tradeoff
explicitly:

- Non-rotating mode: keep the warm pool (current behaviour, fast).
- Rotating mode: context lifetime equals proxy lease. Pool warms *browsers*,
  not contexts.

Add `is_proxy_error(exc)` — pattern-matching `net::ERR_PROXY*`, `ERR_TUNNEL*`,
connection-refused/reset/timeout — so proxy failure is distinguishable from
target failure in retry and in the health score. Without it, a dead proxy
scores against the persona.

**Deliverables**
- [ ] Context lifetime bound to proxy lease in rotating mode; documented tradeoff
- [ ] `is_proxy_error` in `src/domain/`; retry and session scoring consume it
- [ ] Test: two leases with different proxies do not share cookies

---

### A3 `AMENDS P3` — Politeness needs adaptive delay, not just a semaphore

P2 feeds `crawl-delay` into "the existing per-domain rate limiter", which is
`DomainRateLimiter(default_budget=2)` — a fixed concurrency semaphore with no
delay, no backoff, and no `Retry-After` handling. Robots' `Crawl-delay` is a
floor, not a controller. A 429 currently changes nothing about subsequent
behaviour.

**Design — `AutoThrottle` as a pure controller (Principle 3).**

Per domain, converging on observed latency:

```
target  = latency / target_concurrency
delay   = max((current + target) / 2, target)          # damped convergence
penalty = retry_after if blocked and retry_after       # server's number wins
          else current * 2 if blocked
delay   = clamp(max(delay, penalty, current), floor, max_delay)
```

The `max(..., current)` term is the important one: **a block can never speed
the crawler up.** `floor` is `max(configured_delay, robots_crawl_delay,
robots_request_rate)` from P2.

Pure function over `(domain_state, latency, ok, retry_after) -> new_state`,
in `src/domain/`. State persists on `DomainProfile` (Principle 4), so the
learned delay is shared cluster-wide rather than per-worker.

`parse_retry_after` handles both integer-seconds and HTTP-date forms, clamps
negatives to zero, and logs-and-ignores malformed values.

**Deliverables**
- [ ] `throttle.py` in `src/domain/` — pure, mypy-strict
- [ ] `parse_retry_after` with both forms + malformed handling
- [ ] Delay persisted on `DomainProfile`; both fetch tiers consult it
- [ ] Test: sequence of (fast, fast, 429 with Retry-After: 30, fast) never drops below 30 on the fourth call

---

### A4 `AMENDS P4` — Specify the element signature and the match

P4 names element signatures ("tag, classes, text head, parent chain hash") and
"Scrapling's relocation idea". A hash-based signature cannot do fuzzy
relocation — a hash either matches or does not, which is the failure mode that
made the selector break in the first place.

**Design — signature as a comparable structure, match as a graded score.**

*Signature* (per overlay field, stored alongside `field_mappings`):
tag; full attribute dict; own text; the **tuple of ancestor tags to root**;
parent tag, attributes and text; sibling tag tuple; child tag tuple. No single
field is trusted — the redundancy is the point, because each survives a
different kind of redesign.

*Match* — normalized multi-signal similarity, accumulating `score` and
`checks`, returning `score / checks`:

| Signal | Contribution |
|---|---|
| tag equality | binary |
| text | `SequenceMatcher` ratio, only if original had text |
| attribute dict | 0.5 × key-sequence ratio + 0.5 × value-sequence ratio |
| `class`, `id`, `href`, `src` | separate ratio each — deliberately double-counted; these are identity-bearing and carry full structural changes |
| ancestor path tuple | `SequenceMatcher` ratio |
| parent tag / attrs / text | one check each |
| sibling tuple | `SequenceMatcher` ratio |

Dividing by `checks` rather than a fixed denominator means an element with no
text and no parent is not penalised for signals it never had.

*Relocation* scores every candidate element, buckets by score, returns the
highest bucket above threshold (start at 45; tune on fixtures). Do **not**
early-exit on a perfect score — ties are the normal case for list items.

**Schema change required.** Verified: `extraction_overlays` has
`container_selector TEXT` and `field_mappings TEXT` and **no signature
column**; `ExtractionOverlay` has no signature field. This needs a migration
adding `field_signatures TEXT` (JSON) plus the model field. An earlier draft
of this analysis incorrectly claimed the existing table could hold them.

**Ladder position.** Relocation sits **above** the LLM in P4's repair ladder,
and it only runs on the miss path — a healthy overlay pays nothing:

```
1. selector resolves                  → done, refresh signature
2. selector misses, signature relocates → extract, write back regenerated
                                          selector as CANDIDATE
3. relocation below threshold          → content-addressed fallback (P7)
4. all deterministic paths exhausted   → heal_selector / autograph (existing)
```

**Also amends P4's cost metric.** P4 tracks "LLM calls per 1000 pages". Add
"deterministic repair rate" — repairs resolved at rungs 2–3 — because that is
the number this design is trying to move.

**Deliverables**
- [ ] Migration + model field for `field_signatures`
- [ ] Signature capture on overlay creation and on every successful extraction (keeps it fresh)
- [ ] Similarity scorer as a pure function in `src/domain/`, mypy-strict
- [ ] Relocation at ladder rung 2; regenerated selectors enter as CANDIDATE only
- [ ] Test: fixture pair (original page, redesigned page with changed classes and reordered DOM) — relocation recovers ≥ 80% of fields with zero LLM calls

---

## 6. Phase P7 — Extraction Substrate

**Goal:** replace the parser, and add the selection methods that make the LLM
unnecessary for the common cases. Nothing in 08-13 covers this; P4 assumes the
current parser.

**Entry:** P0. **Parallel with:** P1, P2.

### P7.1 — Parser replacement

`extraction_pipeline.py:62` uses `BeautifulSoup(html, "html.parser")` — the
pure-Python parser — and every dispatch stage re-walks that tree.
`html.parser` is the slowest option available in the stack; lxml is C.

**Honest scoping.** Scrapling's published benchmark (≈785× on a 5000-element
text-extraction microbenchmark) measures a different operation than this
pipeline performs and should not be quoted as an expected end-to-end speedup.
**Measure first**: profile one representative job before and after. The change
is justified on the parser being strictly better on every axis (speed,
standards-compliance, XPath support), not on a borrowed number.

`Selector` supports `find_all(tag, class_=...)` in BeautifulSoup's shape as
well as CSS and XPath, so most call sites migrate mechanically.

Per Principle 8: **no `SelectorPort`.** One implementation, pure operation,
already testable. Import it directly.

**Result (2026-08-20):** Done. All call sites in `extraction_pipeline.py`
migrated (`BeautifulSoup(html, "html.parser")` → `Selector(html)`; `.select`/
`.select_one` → `.css`/`.css().first`; `.get_text(strip=True)` →
`.get_all_text(separator="", strip=True)`, verified byte-identical output on
a bs4-vs-scrapling comparison; `.find_parent(tags)` → `.find_ancestor(lambda
n: n.tag in tags)`). Profiled article+table+list extraction on a synthetic
200-block/56KB representative page, 5-run median: bs4 1.14s → scrapling
0.37s, **3.06×** — real number, not the borrowed 785× benchmark. Existing
11-test `test_extraction_pipeline.py` suite plus the full repo suite (284
passed, only the 12 pre-existing/unrelated `test_api_smoke.py` import errors)
pass unchanged, serving as the R11 golden-fixture regression check. `scrapling`
added to `requirements.txt` (base only, no `[fetchers]` extra, per §3).

### P7.2 — Content-addressed selection ladder

The current fallback chain is JSON-LD → `<article>` → `<table>` → list. It has
no way to find data by what it *looks like*, which is why unknown domains
escalate straight to Gemini.

Add, as a Chain of Responsibility (Principle 2) after the existing stages:

- **Microdata / RDFa / OpenGraph** — structured, deterministic, currently missing.
- **`find_by_regex`** — locate by content shape (prices, dates, IDs), then walk
  to the container and take its full text. Handles the split-span case
  (`<span>$</span><span>45,000</span>`) that defeats selector-based extraction.
- **`find_similar`** — given one located element, find its peers: same depth,
  same tag, same parent and grandparent tag, then attribute similarity.
  Ignore `href`/`src` by default (they legitimately differ between siblings)
  and use `max(len(original_attrs), len(candidate_attrs))` as the denominator
  so a candidate with extra attributes is penalised rather than inflated.
- **Selector synthesis** — `generate_css_selector` on a located element, so a
  successful content-addressed extraction *becomes* a candidate overlay. This
  is the cheap path to overlay bootstrapping that currently costs an LLM call.

**Result (2026-08-21):** Done, as two new Chain-of-Responsibility stages
after semantic HTML, both gated on `if not all_records` (last resort before
Gemini). **Stage G — structured markup:** OpenGraph (`meta[property^="og:"]`),
then Microdata (`[itemscope]`/`[itemprop]`/`itemtype`), then RDFa
(`[typeof]`/`[property]`) — first one with hits wins; nested scopes fold into
the parent record instead of double-counting. **Stage H — content-addressed:**
`find_by_regex` against a small price/date pattern table locates a match,
`.parent` recombines split-span values (verified on the `<span>$</span>
<span>45,000</span>` case from the plan), `find_similar` finds >= 2 peers
(< 3 total items is treated as noise, not a list). A hit is synthesized into a
CANDIDATE overlay via `overlay_repo.create_overlay` — container_selector from
the containers' shared class (falling back to `generate_css_selector`),
field_mappings from the matched element's tag+class. Never touches ACTIVE
state directly; promotion still gated by `ShadowOverlayEvaluator` on real
evidence, same path as an LLM-authored overlay (R13). 6 new tests in
`test_extraction_pipeline.py` (opengraph, microdata, nested-scope, repeating
list, single-match-is-not-a-list, overlay-synthesis-shape); full suite still
green (396 passed).

### P7.3 — Fix the list-noise rule

`extraction_pipeline.py:277-287` emits a `record_type="list"` record for any
`<ul>`/`<ol>` with ≥ 3 `<li>`. That fires on essentially every navigation menu,
footer, and breadcrumb on the web, and those records then flow into
deduplication, persistence, and (once P2 lands) link discovery.

Scope it: require a `main`/`article` ancestor, or exclude `nav`/`footer`/
`header`/`aside` ancestors; require the items to carry more than a bare link.

**Deliverables**
- [ ] Profile captured before/after on a representative job; result recorded in the PR
- [ ] `Selector` replaces BeautifulSoup across the extraction pipeline; no `SelectorPort`
- [ ] Microdata/RDFa/OpenGraph stages
- [ ] `find_by_regex` / `find_similar` / selector-synthesis stage, feeding CANDIDATE overlays
- [ ] List rule scoped; regression fixture asserting a nav menu yields zero records
- [ ] Contract fixtures (P4's harness) extended to cover each new stage

---

## 7. Phase P8 — Agent Surface and Export

**Goal:** make the cluster usable by coding agents and make results portable.
08-13 P5 covers HTTP verbs; this covers the agent protocol and export.

**Entry:** P5.

### P8.1 — MCP server

`cli.py` is already a good agent surface (JSON on stdout, logs on stderr,
meaningful exit codes). MCP is the protocol agents actually speak, and this
project has the three things an MCP scraping server needs and that
library-only implementations have to fake: authentication, job lifecycle, and
record storage.

Thin adapter over the P5 application services — **no new business logic**
(thin controllers, per 08-13 P5's own rule). Tools: `scrape`, `crawl`, `map`,
`extract`, `get_job`, `get_records`.

Two design constraints:

- **Return sanitized markdown, not raw HTML.** P4's `to_markdown` compactor
  mode plus S5's sanitizer. An agent billed for raw markup is the failure mode
  this whole line of work exists to avoid.
- **Reuse the existing API-key auth.** Bearer token compared with
  `hmac.compare_digest`. Do not invent a second auth path (Principle 6).

### P8.2 — Export primitives

Add `to_json` / `to_jsonl` / `to_csv` / `to_xml` on the record list. Small, but
the two things that break real exports need handling:

- **XML**: strip characters outside the legal XML range; rewrite invalid tag
  names and preserve the original in a `name` attribute.
- **CSV**: serialize non-scalar cells as JSON rather than `str(dict)`; union
  the key set across heterogeneous records so nothing is silently dropped.

**Deliverables**
- [ ] MCP server as an adapter over P5 services; existing auth reused
- [ ] Markdown output path sanitized (S5) and compacted (P4)
- [ ] Four exporters with XML/CSV edge cases covered
- [ ] Test: record containing control characters and a nested dict round-trips through all four formats

---

## 8. Security and Safety

Security items are called out separately because several cross phase
boundaries and because two are new findings.

### SEC-1 — SSRF must survive the transport change (specifies 08-13 R2)

R2 identifies the risk. The design is unspecified, and it is not trivial:
`SSRFValidatingTransport` is an **httpx transport hook**. `curl_cffi` has no
equivalent, so P1 Tier 1 would silently bypass every SSRF control — including
the DNS-rebinding closure that is currently the strongest thing in
`src/security/`.

**Design — validation is a domain gate, enforcement is per-transport.**

```
src/domain/net_guard.py        (pure; no I/O)
    def classify_target(host, resolved_ips) -> Verdict   # allow | deny(reason)

src/security/                  (I/O; one enforcement adapter per transport)
    httpx      → existing SSRFValidatingTransport, calls classify_target
    curl_cffi  → resolve-then-pin: resolve the host, classify every returned IP,
                 fail closed on any deny, then connect to the *validated IP*
                 with the Host header preserved
```

Pinning the connection to the already-validated IP is what closes the
rebinding window without a transport hook. Redirects re-enter the gate — the
curl_cffi adapter must disable automatic redirect following and drive hops
itself, or the check applies only to the first hop.

**Fail closed.** An unresolvable host, a resolution error, or a mixed
allow/deny IP set is a deny.

- [ ] `classify_target` pure, mypy-strict, shared by both adapters
- [ ] curl_cffi adapter: resolve → classify all IPs → pin → manual redirect loop
- [ ] Test (both transports): RFC1918, loopback, link-local, IPv6 ULA, cloud metadata IP, and a rebinding fixture all fail closed
- [ ] CI check that no scrape-path client is constructed without an enforcement adapter

### SEC-1b — The SSRF guard defaults to log-only (`CRITICAL`, `NEW`)

Found during verification. `src/security/validating_transport.py:88-93`:

```python
logger.warning(
    "SSRF egress guard (log-only, SSRF_EGRESS_ENFORCE unset): would have "
    "blocked request to %s — %s",
    request.url,
    reason,
)
```

The guard **does not block unless `SSRF_EGRESS_ENFORCE` is set**. Absent that
environment variable it logs and proceeds. Verify the deployment sets it;
`.env.example`, `docker-compose.yml`, and the Dockerfile all need checking.

This inverts the security posture described elsewhere in the codebase, and it
interacts with SEC-1: designing a transport-agnostic gate is moot if the
enforcement flag is off in production.

- [ ] Confirm whether `SSRF_EGRESS_ENFORCE` is set in every deployment path
- [ ] Invert the default — enforce unless explicitly disabled — or fail startup when unset
- [ ] Startup log line stating the guard's mode, at WARNING when not enforcing
- [ ] Test asserting the default construction blocks rather than logs

### SEC-2 — API key transmitted in the URL query string (`NEW`)

`ai/client.py:95` builds `url = f"{url}?key={self.api_key}"`. Query strings
land in access logs, proxy logs, client history, and `Referer` headers.

**Scoped accurately** — an earlier draft overstated the log-leak path:

- `ai/client.py` never logs `url` itself, and has no `exc_info` or
  `logger.exception` anywhere, so there is **no traceback path**. Whether the
  key reaches a log via `{e}` at `:114` depends on the exception type; most
  `httpx.RequestError` subclasses stringify without the URL.
- The **reliable** leak is `validating_transport.py:88-93` (SEC-1b), which logs
  `request.url` verbatim — full query string included — and is attached to the
  very client the AI code uses (`http_client.py:48`). It fires when a
  destination resolves to a private or metadata address.
- Redaction exists but cannot catch this. `sanitize_for_log`
  (`input_sanitizer.py:33`) redacts Bearer tokens, `ss_`-prefixed keys, emails
  and Postgres DSNs — but `_API_KEY_RE` matches only this app's own `ss_`
  format, never a Google `AIza…` key, and no pattern matches a URL query
  parameter. More importantly **`sanitize_for_log` is never called in
  production**: `logger_config.py:40-52` installs only `CorrelationFilter`, and
  a repo-wide grep finds callers only in tests. `redact_pii` operates on dict
  keys and can never touch a URL string.
- `logger_config.py:43-44` writes everything at DEBUG to `logs/trace.log`, so
  anything logged persists unredacted on disk.

**Fix.** Send the key as the `x-goog-api-key` header — the Gemini API accepts
it — so no code path can log it as part of a URL. Then wire `sanitize_for_log`
into the actual handlers (it is currently dead code), and add a query-parameter
redaction pattern.

- [ ] Key moved to header; no secret in any URL, repo-wide
- [ ] `sanitize_for_log` installed as a filter on both handlers in `logger_config.py`
- [ ] Redaction pattern for credential-bearing query parameters (`key=`, `token=`, `api_key=`)
- [ ] Test: a URL containing `?key=AIza…` passed through the logging stack emits no key material

### SEC-3 — Unbounded response buffering (`NEW`, availability)

`engine.py:102-120` calls `await response.json()` on **every** JSON response
and appends it to `self.intercepted_json` with no size cap, no count cap, and
no total-bytes cap. A page that emits large or numerous JSON responses drives
worker memory without bound. The queue has OOM backpressure; this path is
upstream of it.

- [ ] Per-response size cap, per-page count cap, per-page total-bytes cap; all configurable
- [ ] Overflow logged and counted, not silent
- [ ] Test: fixture emitting oversized JSON is truncated and counted, worker survives

### SEC-4 — `bypass_csp=True` on every context

`pool.py:93`. Disabling Content-Security-Policy removes a browser-level
control on what the page may load and execute, in a process that is
deliberately visiting hostile pages. The comment says it "allows for deeper
script interrogation"; nothing in the codebase performs script interrogation.

- [ ] Removed. If a specific target ever needs it, make it an explicit per-job flag with a logged reason.

### SEC-5 — `--no-sandbox` unconditionally

`pool.py:55`. The Chromium sandbox is the primary containment boundary between
hostile page content and the host. Disabling it globally to satisfy a container
constraint applies the weakening everywhere, including local and CI runs.

- [ ] Conditional on a container-detection check or an explicit env flag; off by default
- [ ] Container path documented in `DEPLOYMENT.md` with the residual risk stated

### SEC-6 — Forensic screenshots have no retention or redaction policy

`engine.py:184-196` writes full-page screenshots of scraped pages to
`exports/evidence/` on every failure. Those images can contain personal data
from the target page, and they accumulate unbounded. The codebase redacts PII
before sending text to an LLM but applies nothing here.

- [ ] Retention window with a purge task (reuse the P0 reaper pattern)
- [ ] Off by default in production; on for debug
- [ ] Documented as potentially containing target-page personal data

### SEC-7 — Do not adopt pickle for checkpoints

Scrapling checkpoints with `pickle`. Correct for a single-user library writing
to its own directory; **not** correct here. Pickle deserialization is arbitrary
code execution, and in a multi-tenant cluster a checkpoint file is an
attacker-reachable input.

P6's graceful drain should use the existing durable substrate — Streams pending
entries plus SQLite job state — and keep only the *behaviours* worth copying:
two-stage interrupt (first drains in-flight, second forces) and
callback-restoration by name rather than by reference.

- [ ] Drain uses existing storage; no pickle anywhere in the checkpoint path
- [ ] Two-stage SIGINT/SIGTERM documented and tested

### SEC-8 — Robots fail-closed, and the override is auditable

P2 specifies fail-closed with a bypass flag (08-13 R4). Add: every use of the
bypass is logged with the job ID and the requesting key, so "we own this
target" is an auditable claim.

- [ ] Bypass logged at info with job and key identity

---

## 9. Testing Strategy

Repo baseline: pytest + pytest-asyncio, `tests/` and `tests/integration/`,
80% coverage floor, mypy strict on `src/domain` only.

**Exploit the mypy configuration.** `[tool.mypy] files = ["src/domain"]` with
`strict = true` means anything placed in `src/domain/` gets strict checking for
free. Nearly every new pure component in this plan — `fingerprint`,
`net_guard`, `throttle`, similarity scoring, `sanitize_for_llm`,
`BlockSignalDetector`, `is_proxy_error` — belongs there by Principle 5 and
gains strict typing as a side effect. This is the cheapest quality lever
available and it costs nothing to use.

| Layer | What | Where |
|---|---|---|
| Pure unit | Fingerprint coherence, similarity scoring, throttle convergence, `classify_target`, retry-after parsing, block detection | `tests/` — no I/O, no fixtures, fast |
| Golden fixture | Extraction stages against stored HTML snapshots; redesign pairs for relocation | `tests/contracts/` (P4 creates this harness; P7 and A4 extend it) |
| Contract | Launched-browser fingerprint agreement; SSRF fail-closed on both transports | `tests/integration/` |
| Regression | One per Phase-S defect, asserting the specific broken behaviour | alongside the fix |

**Non-negotiable regression tests** (each encodes a defect that already
shipped):

1. Launched context: UA header, `navigator.userAgent`, `navigator.platform`,
   `Sec-CH-UA-Platform`, and `screen`/`viewport` relationship all agree. (S1)
2. Page URL returns HTML, endpoint returns JSON → one job, one success,
   endpoint promoted, page URL not promoted. (S3)
3. Hidden-div instruction text produces byte-identical model input to the page
   without it. (S5)
4. RFC1918 / metadata-IP / rebinding targets fail closed on **both**
   transports. (SEC-1)
5. Forced AI-client 4xx logs no key material. (SEC-2)
6. Nav-menu fixture yields zero records. (P7.3)

**Coverage gate applies to new code.** Phase S touches existing modules with
existing tests; the 80% floor is measured on the diff, not just globally, so
a large well-covered codebase cannot hide an untested fix.

---

## 10. Sequencing

```
Phase S  (no gate — parallel with P0)
  S7 ai await bug ┐  ← one line; unblocks every AI feature
  S1 fingerprint ─┤
  S2 http split ──┤
  S3 turbo       ─┼─→ independent, any order
  S4 networkidle ─┤
  S5 llm gates   ─┤
  S6 silent fail ─┘
  SEC-1b ssrf enforce ← check first; may already be a live exposure

P0  (08-13 entry gate — unchanged)
 │
 ├─→ P1 adaptive fetch  [+A1 stealth, +A2 proxy isolation, +SEC-1]
 │     └─→ P3 sessions  [+A3 throttle]
 ├─→ P2 crawling        [+SEC-8]
 ├─→ P4 llm economics   [+A4 signatures]
 ├─→ P7 extraction substrate        ← new, parallel with P1/P2
 │
 └─→ P5 api verbs
       └─→ P8 agent surface + export ← new
     P6 operations [+SEC-7]
```

**Recommended first slice.** 08-13 R8 recommends P1 as the largest single win.
That still holds for *capability* — but three things should land before it:

1. **SEC-1b** — one grep of the deployment config. If `SSRF_EGRESS_ENFORCE` is
   unset in production, the SSRF guard is currently advisory and that is a live
   exposure, not a planned improvement. Check before anything else.
2. **S7** — deleting one `await` restores `generate_overlay`, `heal_selector`,
   `enrich_opportunity`, and `compute_embedding`. 08-13's entire P4 is built on
   top of functions that currently always return `None`; that phase cannot be
   validated until this is fixed.
3. **S1 + S2 + S3** — days, not weeks; no gate; none of P0's files. S1 produces
   the `Fingerprint` value object that P1 Tier 2 and P3's `PersonaFactory` both
   consume, so building P1 first means building it twice. S3 stops the system
   burning three failed jobs per domain today.

Order: **SEC-1b → S7 → S1 → S2 → S3 → (S4, S5, S6 any order) → P0 gate →
P1 + P7 in parallel.**

**Effort (relative, consistent with 08-13's scale):**
SEC-1b ≈ XS · S7 ≈ XS · S1 ≈ M · S2 ≈ S · S3 ≈ S · S4 ≈ S · S5 ≈ S · S6 ≈ S ·
A1 ≈ S · A2 ≈ S · A3 ≈ S · A4 ≈ M · P7 ≈ M · P8 ≈ M

**A note on how S7 and S1 escaped notice.** Both are invisible to the test
suite: S7 because the test double implements `async def json()` and the real
`httpx.Response` does not; S1 because a throwing init script produces a
`pageerror` that nothing listens for. Neither is a coverage problem — both
paths *are* covered. They are **fidelity** problems: the test doubles do not
behave like the things they replace. Wherever this plan adds a test against a
mocked browser or HTTP response, the double must be checked against the real
type's contract, not merely against the code under test.

---

## 11. Risk Register (additions to R1–R8)

R1–R8 in the 08-13 plan remain in force. R2 is now specified by SEC-1.

| # | Risk | Phase | Mitigation |
|---|---|---|---|
| R9 | Fingerprint rewrite changes block rate in an unmeasured direction | S1 | Land behind a flag; A/B against the current stack on a fixed target set; `validate_fingerprint` in CI catches incoherence, but only live block rate proves the direction. Rollback = flag flip. |
| R10 | Chromium version drifts on Playwright upgrade, silently re-breaking the UA | S1, A1 | Version read from the driver at startup, never hardcoded. Startup assertion that the built UA major equals the driver major; fail loud on mismatch. |
| R11 | Parser swap changes extraction output subtly (whitespace, entity handling, malformed-HTML recovery) | P7.1 | Golden-fixture diff across the full contract set before merge; treat any output change as a finding requiring sign-off, not a rounding error. |
| R12 | curl_cffi SSRF adapter drifts from the httpx one, leaving one path weaker | SEC-1, P1 | Single shared `classify_target`; one parametrized test suite executed against both adapters; CI check that no scrape client is built without an adapter. |
| R13 | Content-addressed extraction (P7.2) produces plausible-but-wrong records on unknown domains | P7.2 | Emits CANDIDATE overlays only, never ACTIVE; routed through the existing shadow evaluator and human gate, same as LLM-generated ones (08-13 R6 extended to cover this source). |
| R14 | Phase S runs parallel to P0 and the two collide in `worker_scraper.py` | S, P0 | S3 is the only S-item in that file; sequence it against P0's post-processor work or land it first. Others touch `pool/engine/persona/http_client/ai` exclusively. |
| R15 | Removing `--no-sandbox` breaks container runs | SEC-5 | Container detection with explicit opt-out; CI runs both paths; documented before merge. |
| R16 | `scrapling` base dependency pulls a transitive browser install via a future release | §3 | Pin the version; CI check asserting `playwright`/`patchright` appear exactly once in the resolved lock. |

---

## 12. Non-Goals

08-13 §6 non-goals stand unchanged: CAPTCHA solving, browser-agent extraction,
operator dashboard, Postgres migration, Kafka/saga, third fetch tier,
cross-host orchestration.

Added by this plan:

- **`scrapling[fetchers]` as a dependency.** See §3 — the browser-driver cost
  is not worth it when P1/P3 already build the equivalent. Techniques are
  ported; the fetcher stack is not adopted.
- **patchright as a second driver.** Same reason. Revisit only if A1's
  flag-based approach measurably underperforms on block rate.
- **A `SelectorPort` / parser abstraction.** One implementation, pure
  operation, no test seam. Principle 8.
- **Pickle-based checkpointing.** SEC-7.
- **Rewriting `BrowserContextPool` in Phase S.** S1 makes the fingerprint
  coherent within the existing pool; the pool's lifecycle model is P3's to
  change (A2).

---

## Appendix A — Verification Record and Corrections

Every factual claim in the source study was re-checked against source by an
independent verification pass (57 claims across six bundles), followed by an
adversarial pass instructed to refute the high-stakes survivors. Corrections:

### Refuted outright — do not publish or act on

| Claim | Finding |
|---|---|
| "Impossible viewport/screen pair" | **False.** Playwright's `set_viewport_size` sets `screen` to the viewport (`coreBundle.js:21764`). No anomaly. |
| "Four dead persona fields" | **Overstated.** Only `canvas_bitmask` and `audio_pinnacle` are dead. `has_touch` is a no-op default; `device_scale_factor` is load-bearing on the RNG stream — deleting it changes every persona's WebGL fingerprint and invalidates StealthBrain scores. |
| "`webdriver` patched twice to opposite values" | **Mechanism wrong, conclusion worse.** The second define *throws*, killing the rest of the evasion script. Restated in S1. |
| "WebGL disabled then faked" | **Impact wrong.** With both flags, `getContext('webgl')` and `('webgl2')` both return `null`. The spoof is unreachable; the absence of WebGL is the actual signal. |
| "`grep @app.get` returns nothing, so those routes may not exist" | **My grep was malformed.** `main.py` has 13 `@app.*` decorators. `GET /jobs/{job_id}` is at `main.py:434`, `GET /jobs/{job_id}/records` at `:497`. No `APIRouter` anywhere. 16 routes total. |
| "`--disable-features` passed twice, so TranslateUI is lost" | **Understated.** Playwright *prepends* its own 17-entry default; caller args land last, so `IsolateOrigins` discards Playwright's entire default set too. |

### Corrected

| Claim | Correction |
|---|---|
| "Adopt Scrapling as the fetch layer" | Withdrawn — uncosted (two browser drivers) and conflicts with P1's `curl_cffi` decision. See §3. |
| "Fingerprints live alongside selectors in the existing table" | **False, confirmed.** `extraction_overlays` has 13 columns, none for signatures (`overlay_repository.py:24-40`); `ExtractionOverlay` has 13 fields, none for signatures (`models.py:196-213`). Migration required — A4. |
| "785× slower is the largest single CPU line item" | Unmeasured; the benchmark measures a different operation. P7.1 requires a before/after profile. |
| "~8 of 19 findings were already in the 08-13 plan" | **Wrong in the other direction.** Verified: **3 fully recorded** (G4, G10, G11), **6 partial** (G1, G2, G5, G7, G12, G13), **10 not recorded** (G3, G6, G8, G9, G14–G19). The self-criticism over-corrected. |
| "SSRF unaddressed in the recommendation" | 08-13 R2 flagged it; now specified in SEC-1 — and see SEC-1b, which is more urgent. |
| Scrapling "15,400 lines" | Package is 15,444 across 57 files; `ad_domains.py` is 3,537 of them, so **11,907 lines of code**. |
| Spacescraper "9,700-line cluster" | `src/` is 9,715 across 78 files; root entrypoints add 1,956 (`main.py` 643, `cli.py` 372, `worker_scraper.py` 425, `worker_processor.py` 219, `worker_reporter.py` 103, `spacescraper.py` 120, `boot.py` 74) — **11,671 total**. |

### Confirmed, and new

- **S7** — `await response.json()` breaks every AI call. Not in the original study.
- **SEC-1b** — the SSRF guard is log-only unless `SSRF_EGRESS_ENFORCE` is set.
- **`sanitize_for_log` is dead code** — defined at `input_sanitizer.py:33`, called only from tests. `logger_config.py:40-52` installs only `CorrelationFilter`.
- **`src/bootstrap.py` is imported by nothing** — not `main.py`, not any worker, not any test. The "single composition root" docstring is aspirational; every entrypoint constructs its own adapters. This is 08-13 P0's job, and it means anything this plan wires "via the composition root" must first give that module a caller.
- **`ports.py` has exactly 7 Protocols** — `JobRepository`, `RecordRepository`, `OutboxRepository`, `OverlayRepository`, `ApiKeyRepository`, `MessageBus`, `ObservationRepository`. No `FetcherPort`, `RobotsPort`, or `LLMProviderPort`.
- **Exactly one import-linter contract** — `src.domain` may not import `src.application` or `src.infrastructure`. Nothing constrains the reverse.
- Confirmed: 30/30 `migration_*.log` are zero bytes; no `LICENSE` at repo root; Scrapling is BSD-3 (Karim Shoair); `extraction_pipeline.py:62` is the sole `BeautifulSoup` construction; `follow_urls = []` at `:358`.
- Confirmed unchanged: SEC-2 (key in URL), SEC-3 (unbounded buffering), SEC-4 (`bypass_csp` with no code justifying it), SEC-5 (`--no-sandbox` with no guard anywhere), SEC-6 (screenshots, no retention). SEC-2/3/5/6 additionally survived the adversarial pass.

### Verification gap

The adversarial re-check of SEC-4 (`bypass_csp`) failed with a connection
error and did not complete. The base verdict is CONFIRMED with strong evidence
(repo-wide grep finds no script-interrogation code justifying it), but it has
not been through the refutation pass that the other security findings passed.
