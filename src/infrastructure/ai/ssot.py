# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (AI Single Source of Truth)
# Role: The ONLY place in the codebase where AI model ids, endpoints, prompt
#       budgets, timeouts, temperatures and resilience constants are written.
#
# Rule: no module outside this file may hardcode a model id, an API endpoint, a
# timeout, a retry count, a cache size, a temperature or a prompt character
# budget. Import from here instead. `tests/test_ai_ssot.py` enforces the rule by
# scanning the source tree, so a violation fails CI rather than drifting.
#
# ---------------------------------------------------------------------------
# Model selection provenance
# ---------------------------------------------------------------------------
# Every id below was chosen from the live OpenRouter catalogue
# (`scripts/refresh_openrouter_models.py`, snapshot in `models.json`), ranked by
#
#     value = benchmarks.artificial_analysis.intelligence_index / blended $/1M
#     blended $/1M      = 0.75 * prompt + 0.25 * completion
#
# The input weighting reflects the real workload: every job sends a large prompt
# (compacted HTML, or a record's fields) and gets back a small JSON object.
# `intelligence_index` is OpenRouter's own published benchmark, not an estimate.
# Re-run `scripts/refresh_openrouter_models.py --rank` to reproduce the table;
# `--verify` re-checks these pins against the live catalogue.
#
# ---------------------------------------------------------------------------
# Three catalogue facts that drive the shape of this file
# ---------------------------------------------------------------------------
# 1. Reasoning tokens bill as OUTPUT tokens, and 109/424 models enable reasoning
#    by default (97 make it mandatory). A model that reasons by default costs far
#    more than its completion price implies, so every job states explicitly
#    whether it wants reasoning instead of inheriting the vendor default.
# 2. 81/424 models reject `temperature` outright (59 OpenAI, 12 Anthropic,
#    4 Google) — OpenAI's reasoning models run at a fixed temperature of 1. The
#    parameter is therefore emitted only when the model advertises support, and
#    fallbacks are restricted to temperature-accepting models so one payload
#    stays valid across a whole fallback chain.
# 3. The catalogue contains zero embedding models, so with everything routed
#    through OpenRouter there is no embedding job at all and deduplication
#    falls back to fuzzy title matching.
# 4. Both primaries cache prompts automatically — DeepSeek reads cached input at
#    0.1x, Z.AI at ~0.2x — with no cache_control breakpoints required. That
#    discount is contingent on prompt layout; see PROMPT_CACHE_NOTE.

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

PROMPT_CACHE_NOTE: Final = """
Automatic prompt caching only hits when the *stable* part of a prompt comes
first and the variable part last, because providers cache on a prefix match.
Every prompt builder in the OpenRouter adapter is written that way: fixed
instructions and output schema first, then the page HTML or record fields.

Reordering a prompt to interpolate variable data near the top silently stops
the cache matching. Nothing breaks and no test fails — the bill just goes up
5-10x on the affected job. Keep variable content last.
"""

# Weights used by both the ranking script and ModelSpec.blended_price_per_m.
PRICE_BLEND_IN: Final[float] = 0.75
PRICE_BLEND_OUT: Final[float] = 0.25


class AIJob(str, Enum):
    """The distinct LLM workloads. Each gets its own model, budget and knobs."""

    OVERLAY = "overlay"    # HTML page -> declarative extraction schema
    ENRICH = "enrich"      # record -> translation, summary, normalised budget
    HEAL = "heal"          # HTML chunk -> one repaired CSS selector
    GENERATE = "generate"  # free-form text, the EnrichmentProvider port default
    SEARCH = "search"      # query -> ranked URLs, via OpenRouter's web-search server tool


@dataclass(frozen=True)
class ModelSpec:
    """A pinned model plus the catalogue evidence behind the pin."""

    id: str
    context_length: int
    price_in_per_m: float             # USD per 1M prompt tokens, snapshot
    price_out_per_m: float            # USD per 1M completion tokens, snapshot
    intelligence_index: float | None  # artificial_analysis; None when unpublished
    structured_outputs: bool
    rationale: str
    # Whether the model accepts a `temperature` parameter at all. False for
    # OpenAI reasoning models and most Anthropic models, which run fixed at 1.
    temperature_supported: bool = True
    # Mirrored from the catalogue's `reasoning` block.
    reasoning_default_enabled: bool = False
    reasoning_mandatory: bool = False
    supported_efforts: tuple[str, ...] = ()
    # Prompt caching. Both primary pins cache automatically with no
    # cache_control breakpoints, so the discount is free — provided the prompt
    # keeps its stable prefix first. See PROMPT_CACHE_NOTE.
    prompt_cache_automatic: bool = False
    cache_read_multiplier: float | None = None

    @property
    def blended_price_per_m(self) -> float:
        """Input-weighted price, matching the ranking formula above."""
        return PRICE_BLEND_IN * self.price_in_per_m + PRICE_BLEND_OUT * self.price_out_per_m

    @property
    def value_score(self) -> float | None:
        """Intelligence per blended dollar. None when the model is unbenchmarked."""
        if self.intelligence_index is None or self.blended_price_per_m <= 0:
            return None
        return self.intelligence_index / self.blended_price_per_m


# ---------------------------------------------------------------------------
# Pinned models
# ---------------------------------------------------------------------------
# Ids are pinned deliberately. OpenRouter also publishes floating aliases
# (`~vendor/model-latest`); those are excluded because a silent upgrade would
# change extraction behaviour with no diff and no way to bisect a regression.
# `:batch` tiers are excluded too — they trade latency for price, and every job
# here is on a synchronous request path.

MODEL_OVERLAY: Final = ModelSpec(
    id="z-ai/glm-5.3-flash",
    context_length=1_310_720,
    price_in_per_m=0.075,
    price_out_per_m=0.250,
    intelligence_index=57.5,
    structured_outputs=True,
    temperature_supported=True,
    reasoning_default_enabled=True,
    reasoning_mandatory=True,
    supported_efforts=("max", "high", "low"),
    prompt_cache_automatic=True,
    cache_read_multiplier=0.2,
    rationale=(
        "Highest intelligence_index (57.5) in the sub-$0.15 tier — within 8 points of "
        "the catalogue frontier (claude-fable-5.1, 65.7) at 1/168th the blended price. "
        "Overlay output is cached per page, so call volume is low and correctness "
        "dominates cost: a wrong selector map silently corrupts every record extracted "
        "from that site. The 1.31M context absorbs any compacted page. Reasoning is "
        "MANDATORY here and defaults to 'max' effort (~95% allocation), which would "
        "blow the old 10s budget — the profile pins effort to 'low' and widens the "
        "timeout to match."
    ),
)

MODEL_ENRICH: Final = ModelSpec(
    id="deepseek/deepseek-v4-flash-0731",
    context_length=1_310_720,
    price_in_per_m=0.065,
    price_out_per_m=0.180,
    intelligence_index=51.8,
    structured_outputs=True,
    temperature_supported=True,
    reasoning_default_enabled=True,
    reasoning_mandatory=False,
    supported_efforts=("max", "high", "low"),
    prompt_cache_automatic=True,
    cache_read_multiplier=0.1,
    rationale=(
        "Best value score (552) of any model above 50 on intelligence_index. Enrichment "
        "is the highest-volume job — one call per extracted record — so price per call "
        "dominates, while translation and budget normalisation still need real "
        "competence. Dated pin (-0731) rather than the floating alias, so a vendor "
        "refresh cannot change extraction output without a visible diff. Reasoning "
        "defaults on at 'high' effort but is not mandatory, so the profile disables it: "
        "translation and currency conversion are recall tasks, and reasoning bills as "
        "output on the busiest path in the system."
    ),
)

MODEL_HEAL: Final = ModelSpec(
    id="inclusionai/ling-3.0-flash",
    context_length=262_144,
    price_in_per_m=0.021,
    price_out_per_m=0.063,
    intelligence_index=37.8,
    structured_outputs=False,
    temperature_supported=True,
    reasoning_default_enabled=True,
    reasoning_mandatory=False,
    rationale=(
        "Best value score in the whole catalogue (1200) at a usable intelligence_index "
        "of 37.8. Selector healing is narrow and latency-sensitive — one HTML chunk in, "
        "one CSS selector out — and runs on the failure path, so cheapness and speed "
        "matter more than headroom. 262K context covers the 4K-char chunk many times "
        "over. Reasoning defaults on but is not mandatory; the profile disables it, "
        "since a chain of thought to emit one selector is pure latency."
    ),
)

MODEL_GENERATE: Final = ModelSpec(
    id="deepseek/deepseek-v4-flash-0731",
    context_length=1_310_720,
    price_in_per_m=0.065,
    price_out_per_m=0.180,
    intelligence_index=51.8,
    structured_outputs=True,
    temperature_supported=True,
    reasoning_default_enabled=True,
    reasoning_mandatory=False,
    supported_efforts=("max", "high", "low"),
    rationale="Shares the enrichment pin: the generic port default should be the safe mid-tier workhorse.",
)

# --- fallback models -------------------------------------------------------
# Selected under three constraints, in order:
#   1. different vendor from the primary — a fallback that shares a vendor does
#      not survive the outage it exists for;
#   2. accepts `temperature`, so one payload stays valid whichever model serves;
#   3. intelligence_index no more than 25% below the primary.
# OpenRouter tries them left to right and bills whichever actually answers; the
# response's `model` field reports which one did.

FALLBACK_SOLAR: Final = ModelSpec(
    id="upstage/solar-pro4",
    context_length=524_288,
    price_in_per_m=0.030,
    price_out_per_m=0.120,
    intelligence_index=41.6,
    structured_outputs=True,
    temperature_supported=True,
    reasoning_default_enabled=False,
    reasoning_mandatory=False,
    rationale="Cheapest model in the catalogue with IQ > 40, no reasoning surprise, accepts temperature.",
)

FALLBACK_DEEPSEEK: Final = ModelSpec(
    id="deepseek/deepseek-v4-flash-0731",
    context_length=1_310_720,
    price_in_per_m=0.065,
    price_out_per_m=0.180,
    intelligence_index=51.8,
    structured_outputs=True,
    temperature_supported=True,
    reasoning_default_enabled=True,
    reasoning_mandatory=False,
    supported_efforts=("max", "high", "low"),
    rationale="Highest-value model above IQ 50; different vendor from the overlay primary.",
)

FALLBACK_MINIMAX: Final = ModelSpec(
    id="minimax/minimax-m3",
    context_length=1_048_576,
    price_in_per_m=0.420,
    price_out_per_m=0.840,
    intelligence_index=45.4,
    structured_outputs=True,
    temperature_supported=True,
    reasoning_default_enabled=False,
    reasoning_mandatory=False,
    rationale="Third vendor for the overlay chain, 1M context, no mandatory reasoning.",
)

FALLBACK_GLM: Final = ModelSpec(
    id="z-ai/glm-5.3-flash",
    context_length=1_310_720,
    price_in_per_m=0.075,
    price_out_per_m=0.250,
    intelligence_index=57.5,
    structured_outputs=True,
    temperature_supported=True,
    reasoning_default_enabled=True,
    reasoning_mandatory=True,
    supported_efforts=("max", "high", "low"),
    rationale=(
        "Highest-IQ cheap model, used as the last-resort enrich fallback. Reasoning is "
        "mandatory here, so a fallback to it costs more than the primary — acceptable "
        "on a rare outage path, and the reason it is second rather than first."
    ),
)

MODEL_SEARCH: Final = ModelSpec(
    id="inclusionai/ling-3.0-flash",
    context_length=262_144,
    price_in_per_m=0.021,
    price_out_per_m=0.063,
    intelligence_index=37.8,
    structured_outputs=False,
    temperature_supported=True,
    reasoning_default_enabled=True,
    reasoning_mandatory=False,
    rationale=(
        "Cheapest tool-capable model in the catalogue ($0.032/1M blended). Web search "
        "is a relay job: the model invokes the server-side search tool and the results "
        "come back as url_citation annotations, so almost no generation quality is "
        "required of it. Token cost is dominated by the per-request search fee "
        "(WEB_SEARCH_PRICE_PER_REQUEST_USD) regardless of model, which makes the "
        "cheapest capable model the right pick rather than a compromise."
    ),
)

FALLBACK_MIMO: Final = ModelSpec(
    id="xiaomi/mimo-v2.5",
    context_length=1_050_000,
    price_in_per_m=0.140,
    price_out_per_m=0.280,
    intelligence_index=38.0,
    structured_outputs=True,
    temperature_supported=True,
    reasoning_default_enabled=False,
    reasoning_mandatory=False,
    rationale="Third vendor for the heal chain at the primary's capability level.",
)

# Gemini is reachable only through OpenRouter, under its catalogue ids
# (google/gemini-3.8-flash, google/gemini-3.6-flash, google/gemini-2.5-flash-lite,
# ...). There is deliberately no direct generativelanguage.googleapis.com path:
# one provider account, one set of credentials, one place where model choice and
# spend are visible. To put a job on Gemini, override it with an OpenRouter id:
#
#     AI_MODEL_OVERLAY=google/gemini-3.8-flash
#
# Note that most Gemini models on OpenRouter make reasoning mandatory, so they
# cost more than their completion price suggests — see fact 1 above.
#
# There is no embedding job. The catalogue contains no embedding models at all
# (424/424 are chat-completion models, none with an embedding output modality)
# and there is no embeddings route on the chat API. Routing everything through
# OpenRouter therefore means embeddings are unavailable, and deduplication falls
# back to fuzzy title matching. That is a deliberate trade, not an oversight:
# restoring embeddings means adding a second provider account.


@dataclass(frozen=True)
class JobProfile:
    """Everything one LLM call needs, other than the prompt itself."""

    job: AIJob
    model: ModelSpec
    timeout_s: float
    max_prompt_chars: int
    temperature: float
    expects_json: bool
    # Tried left to right when the primary errors, rate-limits or is down.
    fallbacks: tuple[ModelSpec, ...] = ()
    # Whether this job wants a reasoning trace. When False and the model makes
    # reasoning mandatory, the adapter requests the cheapest supported effort
    # instead — it cannot switch it off.
    reasoning: bool = False
    reasoning_effort: str | None = None

    @property
    def model_chain(self) -> tuple[str, ...]:
        """Primary first, then fallbacks — the OpenRouter `models` array."""
        return (self.model.id,) + tuple(f.id for f in self.fallbacks)

    @property
    def temperature_allowed(self) -> bool:
        """True only when every model in the chain accepts `temperature`.

        One payload is sent for the whole chain, so a single model that rejects
        the parameter means it must be omitted for all of them.
        """
        return self.model.temperature_supported and all(
            f.temperature_supported for f in self.fallbacks
        )


# ---------------------------------------------------------------------------
# Per-job profiles
# ---------------------------------------------------------------------------
# Temperature per phase. These are task-shape decisions, not benchmark results:
# extraction, translation and selector repair each have one correct answer, so
# sampling entropy can only hurt. Only the free-form GENERATE job keeps a
# conversational temperature.
#
#   overlay  0.0  one correct selector map; determinism also keeps the per-page
#                 cache meaningful, since two runs on one page must agree
#   enrich   0.2  translation and currency normalisation are near-deterministic;
#                 a small nonzero value keeps the 2-sentence summary readable
#                 rather than clipped
#   heal     0.0  one CSS selector, no creativity wanted
#   generate 0.7  free-form port default, unchanged from the previous behaviour
#
# Timeouts and char budgets are otherwise the values each call site previously
# hardcoded, carried over unchanged so this refactor does not silently alter
# runtime behaviour beyond the model swap. The one deliberate change is the
# overlay timeout: its model reasons mandatorily, which the old 10s did not
# allow for.

JOB_PROFILES: Final[dict[AIJob, JobProfile]] = {
    AIJob.OVERLAY: JobProfile(
        job=AIJob.OVERLAY,
        model=MODEL_OVERLAY,
        fallbacks=(FALLBACK_DEEPSEEK, FALLBACK_MINIMAX),
        timeout_s=45.0,
        max_prompt_chars=6_000,
        temperature=0.0,
        expects_json=True,
        # Reasoning is mandatory on the primary and genuinely useful here: this
        # is the one job where a wrong answer corrupts a whole site's records.
        # 'low' rather than the model's 'max' default caps the token burn.
        reasoning=True,
        reasoning_effort="low",
    ),
    AIJob.ENRICH: JobProfile(
        job=AIJob.ENRICH,
        model=MODEL_ENRICH,
        fallbacks=(FALLBACK_SOLAR, FALLBACK_GLM),
        timeout_s=15.0,
        max_prompt_chars=8_000,
        temperature=0.2,
        expects_json=True,
        reasoning=False,
    ),
    AIJob.HEAL: JobProfile(
        job=AIJob.HEAL,
        model=MODEL_HEAL,
        fallbacks=(FALLBACK_SOLAR, FALLBACK_MIMO),
        timeout_s=10.0,
        max_prompt_chars=4_000,
        temperature=0.0,
        expects_json=False,
        reasoning=False,
    ),
    AIJob.GENERATE: JobProfile(
        job=AIJob.GENERATE,
        model=MODEL_GENERATE,
        fallbacks=(FALLBACK_SOLAR,),
        timeout_s=15.0,
        max_prompt_chars=8_000,
        temperature=0.7,
        expects_json=False,
        reasoning=False,
    ),
    AIJob.SEARCH: JobProfile(
        job=AIJob.SEARCH,
        model=MODEL_SEARCH,
        fallbacks=(FALLBACK_SOLAR,),
        # A server-side search round trip plus generation is far slower than a
        # plain completion; the other Discovery adapters use 10s, which is not
        # enough here.
        timeout_s=30.0,
        max_prompt_chars=2_000,
        temperature=0.0,   # relaying citations, not composing prose
        expects_json=False,
        reasoning=False,
    ),
}


# ---------------------------------------------------------------------------
# Web search (Discovery)
# ---------------------------------------------------------------------------
# OpenRouter's server-tool syntax. The older `plugins: [{"id": "web"}]` form and
# the `:online` model suffix are both deprecated and deliberately not used.

WEB_SEARCH_TOOL_TYPE: Final[str] = "openrouter:web_search"

# Billed per search request, on top of tokens. $0.007 is the Exa
# instant/fast/auto rate — the most expensive of the commonly-routed engines
# (Parallel $0.001-0.005, Perplexity $0.005) — so budgeting against it is the
# conservative choice. This is a real cost exposure: unlike the token cost, it
# is charged per call regardless of how small the result set is.
WEB_SEARCH_PRICE_PER_REQUEST_USD: Final[float] = 0.007

# Hard ceiling on results requested in a single search, independent of what a
# caller asks for. Discovery's own max_fanout caps how many hits can become
# jobs; requesting more than that from a billed API is spend with no possible
# effect on the outcome.
WEB_SEARCH_MAX_RESULTS_CAP: Final[int] = 10

WEB_SEARCH_CACHE_MAXSIZE: Final[int] = 200


def profile_for(job: AIJob) -> JobProfile:
    """Look up a job profile, honouring a per-job model override.

    `AI_MODEL_OVERLAY`, `AI_MODEL_ENRICH`, ... let an operator repin one job
    without editing this file. The override replaces only the id: the recorded
    price and benchmark describe the pinned model, so they are cleared rather
    than left claiming evidence for a model nobody measured. Fallbacks are
    dropped too, since their vendor-independence was chosen against the original
    primary. `temperature_supported` is assumed True for an override — an
    operator naming an OpenAI reasoning model must also set
    `AI_TEMPERATURE_UNSUPPORTED=1`.
    """
    base = JOB_PROFILES[job]
    override = os.environ.get(f"AI_MODEL_{job.value.upper()}")
    if not override or override == base.model.id:
        return base
    replacement = ModelSpec(
        id=override,
        context_length=base.model.context_length,
        price_in_per_m=0.0,
        price_out_per_m=0.0,
        intelligence_index=None,
        structured_outputs=base.model.structured_outputs,
        temperature_supported=os.environ.get("AI_TEMPERATURE_UNSUPPORTED") != "1",
        rationale=f"Operator override via AI_MODEL_{job.value.upper()}; unmeasured.",
    )
    return JobProfile(
        job=base.job,
        model=replacement,
        fallbacks=(),
        timeout_s=base.timeout_s,
        max_prompt_chars=base.max_prompt_chars,
        temperature=base.temperature,
        expects_json=base.expects_json,
        reasoning=base.reasoning,
        reasoning_effort=base.reasoning_effort,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Endpoints:
    openrouter_chat: str = "https://openrouter.ai/api/v1/chat/completions"
    openrouter_catalogue: str = "https://openrouter.ai/api/v1/models"


ENDPOINTS: Final = Endpoints()


# ---------------------------------------------------------------------------
# Resilience and cache policy
# ---------------------------------------------------------------------------
# Previously duplicated verbatim across client.py and openrouter.py.

def _int_env(name: str, fallback: int) -> int:
    """Read an int env var, falling back on absent or malformed values."""
    raw = os.environ.get(name)
    if raw is None:
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


@dataclass(frozen=True)
class ResiliencePolicy:
    breaker_threshold: int = 5
    cooldown_period_s: float = 300.0
    max_retries: int = 3
    base_delay_s: float = 1.0
    max_concurrency: int = 10
    local_provider_timeout_s: float = 30.0

    @classmethod
    def from_env(cls) -> ResiliencePolicy:
        return cls(max_concurrency=_int_env("AI_MAX_CONCURRENCY", cls.max_concurrency))


@dataclass(frozen=True)
class CachePolicy:
    result_cache_maxsize: int = 500
    embedding_cache_maxsize: int = 500
    embedding_key_chars: int = 2_000

    @classmethod
    def from_env(cls) -> CachePolicy:
        return cls(
            result_cache_maxsize=_int_env("AI_RESULT_CACHE_SIZE", cls.result_cache_maxsize),
            embedding_cache_maxsize=_int_env("AI_EMBEDDING_CACHE_SIZE", cls.embedding_cache_maxsize),
        )


RESILIENCE: Final = ResiliencePolicy.from_env()
CACHE: Final = CachePolicy.from_env()



# ---------------------------------------------------------------------------
# Catalogue snapshot
# ---------------------------------------------------------------------------

CATALOGUE_PATH: Final[Path] = Path(__file__).with_name("models.json")


def pinned_openrouter_models() -> tuple[str, ...]:
    """Every OpenRouter id this file pins, for the refresh script's --verify pass."""
    ids: list[str] = []
    for profile in JOB_PROFILES.values():
        for model_id in profile.model_chain:
            if model_id not in ids:
                ids.append(model_id)
    return tuple(ids)
