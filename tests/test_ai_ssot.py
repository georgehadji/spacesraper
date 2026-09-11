# Enforcement tests for the AI single source of truth.
#
# The SSOT is only worth having if a stray literal fails CI rather than
# accumulating quietly, so the first test here greps the source tree. The rest
# pin the three catalogue facts that drove the model choices: reasoning bills as
# output, 81/424 models reject `temperature`, and OpenRouter serves no
# embeddings.

import json
import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from src.infrastructure.ai.ssot import (
    CACHE,
    CATALOGUE_PATH,
    ENDPOINTS,
    JOB_PROFILES,
    RESILIENCE,
    AIJob,
    pinned_openrouter_models,
    profile_for,
)

SRC = Path(__file__).resolve().parent.parent / "src"
SSOT = SRC / "infrastructure" / "ai" / "ssot.py"

# A "vendor/name" string. Matching that shape alone is too loose — it also hits
# relative paths like "logs/trace.log" — so a hit only counts as a model id when
# its vendor appears in the real catalogue snapshot.
VENDOR_SLASH_NAME = re.compile(r"[\"']([a-z0-9][a-z0-9\-]*/[a-z0-9][a-z0-9.\-]*)[\"']")
# LLM provider endpoints only. A bare `googleapis.com` also matched unrelated
# Google services -- Maps/Places among them -- which the AI SSOT has no claim
# over; the invariant being protected is that no second *model* adapter routes
# around ssot.py, so the AI hosts are named explicitly.
AI_PROVIDER_HOSTS = (
    r"openrouter\.ai",
    r"generativelanguage\.googleapis\.com",
    r"aiplatform\.googleapis\.com",
    r"[a-z0-9.\-]*\.openai\.azure\.com",
    r"api\.openai\.com",
    r"api\.anthropic\.com",
)
API_URL = re.compile(
    r"[\"']https?://[^\"']*(?:" + "|".join(AI_PROVIDER_HOSTS) + r")[^\"']*[\"']"
)


def _catalogue_vendors() -> set[str]:
    """Vendor prefixes published by OpenRouter, e.g. {'openai', 'z-ai', ...}."""
    if not CATALOGUE_PATH.exists():
        pytest.skip("catalogue snapshot missing; run scripts/refresh_openrouter_models.py")
    catalogue = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    return {
        m["id"].split("/")[0].lstrip("~")
        for m in catalogue.get("models", [])
        if m.get("id") and "/" in m["id"]
    }


def _python_sources() -> list[Path]:
    return [
        p for p in SRC.rglob("*.py")
        if p != SSOT and "__pycache__" not in p.parts
    ]


def test_no_model_ids_outside_ssot():
    """Only ssot.py may name a model."""
    vendors = _catalogue_vendors()
    offenders: list[str] = []
    for path in _python_sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("#"):
                continue  # prose may discuss a model by name
            for match in VENDOR_SLASH_NAME.finditer(line):
                candidate = match.group(1)
                if candidate.split("/")[0] not in vendors:
                    continue  # a path or media type, not a model id
                offenders.append(f"{path.relative_to(SRC.parent)}:{lineno}: {candidate}")
    assert not offenders, (
        "Model ids must live in src/infrastructure/ai/ssot.py:\n  " + "\n  ".join(offenders)
    )


def test_no_provider_urls_outside_ssot():
    """Only ssot.py may name a provider endpoint."""
    offenders: list[str] = []
    for path in _python_sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if API_URL.search(line):
                offenders.append(f"{path.relative_to(SRC.parent)}:{lineno}")
    assert not offenders, (
        "Provider endpoints must live in src/infrastructure/ai/ssot.py:\n  " + "\n  ".join(offenders)
    )


def test_every_job_has_a_profile():
    assert set(JOB_PROFILES) == set(AIJob)


@pytest.mark.parametrize("job", list(AIJob))
def test_chat_jobs_have_vendor_independent_fallbacks(job):
    """A fallback sharing the primary's vendor cannot survive a vendor outage."""
    profile = JOB_PROFILES[job]
    assert profile.fallbacks, f"{job.value} has no fallback"
    primary_vendor = profile.model.id.split("/")[0]
    for fallback in profile.fallbacks:
        assert fallback.id.split("/")[0] != primary_vendor, (
            f"{job.value} fallback {fallback.id} shares a vendor with the primary"
        )


@pytest.mark.parametrize("job", list(AIJob))
def test_fallback_chain_agrees_on_temperature(job):
    """One payload serves the whole chain, so temperature support must be uniform.

    A chain mixing models that accept and reject `temperature` would 400 on
    whichever model got the parameter it does not support.
    """
    profile = JOB_PROFILES[job]
    support = {profile.model.temperature_supported} | {
        f.temperature_supported for f in profile.fallbacks
    }
    assert len(support) == 1, f"{job.value} chain disagrees on temperature support"
    assert profile.temperature_allowed is profile.model.temperature_supported


def test_deterministic_jobs_run_at_low_temperature():
    """Extraction, translation and selector repair each have one right answer."""
    assert JOB_PROFILES[AIJob.OVERLAY].temperature == 0.0
    assert JOB_PROFILES[AIJob.HEAL].temperature == 0.0
    assert JOB_PROFILES[AIJob.ENRICH].temperature <= 0.2
    # Only free-form generation keeps a conversational temperature.
    assert JOB_PROFILES[AIJob.GENERATE].temperature > 0.5


def test_high_volume_jobs_do_not_pay_for_reasoning():
    """Reasoning tokens bill as output; only overlay is worth them."""
    assert JOB_PROFILES[AIJob.ENRICH].reasoning is False
    assert JOB_PROFILES[AIJob.HEAL].reasoning is False
    assert JOB_PROFILES[AIJob.GENERATE].reasoning is False
    assert JOB_PROFILES[AIJob.OVERLAY].reasoning is True


def test_overlay_effort_is_supported_and_not_the_expensive_default():
    """The overlay model reasons mandatorily and defaults to 'max' (~95%)."""
    profile = JOB_PROFILES[AIJob.OVERLAY]
    assert profile.model.reasoning_mandatory is True
    assert profile.reasoning_effort in profile.model.supported_efforts
    assert profile.reasoning_effort != "max"
    # Mandatory reasoning costs latency, so the budget must allow for it.
    assert profile.timeout_s >= 30.0


def test_there_is_no_embedding_job():
    """OpenRouter's catalogue has no embedding models, so the job cannot exist.

    Routing everything through OpenRouter means embeddings are unavailable and
    deduplication falls back to fuzzy title matching. If a future catalogue adds
    embedding models this test is the reminder to reinstate the job.
    """
    assert not hasattr(AIJob, "EMBED")


def test_every_pinned_model_is_an_openrouter_id():
    """No direct-vendor ids: Gemini included, everything goes through OpenRouter."""
    for model_id in pinned_openrouter_models():
        assert "/" in model_id, f"{model_id} is not an OpenRouter catalogue id"


def test_model_chain_is_primary_first():
    profile = JOB_PROFILES[AIJob.OVERLAY]
    assert profile.model_chain[0] == profile.model.id
    assert len(profile.model_chain) == 1 + len(profile.fallbacks)


def test_env_override_drops_unverified_evidence(monkeypatch):
    """An override must not inherit the pinned model's price or benchmark."""
    monkeypatch.setenv("AI_MODEL_ENRICH", "some-vendor/experimental-model")
    profile = profile_for(AIJob.ENRICH)
    assert profile.model.id == "some-vendor/experimental-model"
    assert profile.model.intelligence_index is None
    assert profile.model.price_in_per_m == 0.0
    assert profile.fallbacks == ()


def test_env_override_can_declare_no_temperature_support(monkeypatch):
    monkeypatch.setenv("AI_MODEL_HEAL", "openai/some-reasoning-model")
    monkeypatch.setenv("AI_TEMPERATURE_UNSUPPORTED", "1")
    assert profile_for(AIJob.HEAL).temperature_allowed is False


def test_malformed_int_env_falls_back(monkeypatch):
    from src.infrastructure.ai.ssot import CachePolicy

    monkeypatch.setenv("AI_RESULT_CACHE_SIZE", "not-a-number")
    assert CachePolicy.from_env().result_cache_maxsize == CachePolicy.result_cache_maxsize


def test_only_openrouter_endpoints_are_defined():
    """No direct generativelanguage.googleapis.com route may survive.

    Gemini is still reachable, but only as an OpenRouter catalogue id. A second
    endpoint would mean a second credential and a second place spend can hide.
    """
    urls = [v for v in vars(ENDPOINTS).values() if isinstance(v, str)]
    assert urls, "Endpoints exposes no URLs"
    for url in urls:
        assert "openrouter.ai" in url, f"non-OpenRouter endpoint still defined: {url}"
    assert not hasattr(ENDPOINTS, "gemini_base")
    assert not hasattr(ENDPOINTS, "gemini_generate")


def test_policies_are_immutable():
    """Frozen dataclasses: shared config must not be mutable at runtime."""
    with pytest.raises(FrozenInstanceError):
        RESILIENCE.max_retries = 99  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        CACHE.result_cache_maxsize = 1  # type: ignore[misc]
