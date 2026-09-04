# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (AI composition root)
# Role: Instantiate the configured AI provider. The only module that decides
#       which concrete adapter the application talks to.

import logging

from src.config_settings import Settings, get_settings
from src.infrastructure.providers.enrichment_provider import (
    EnrichmentProvider,
    NoOpEnrichmentProvider,
)

logger = logging.getLogger("Spacescraper.ProviderFactory")

PROVIDER_OPENROUTER = "openrouter"
PROVIDER_LOCAL = "local"
PROVIDER_NOOP = "noop"

# Retired provider names, mapped to what replaced them. Gemini is still fully
# available — as OpenRouter catalogue ids (google/gemini-*) — but there is no
# longer a second adapter talking to generativelanguage.googleapis.com directly.
_RETIRED = {"gemini": PROVIDER_OPENROUTER}


def create_ai_provider() -> EnrichmentProvider:
    """
    Build the provider named by `AI_PROVIDER`, degrading to NoOp rather than
    raising when its credentials are missing.

    A provider named but unconfigured is a misconfiguration, not a fallback
    opportunity: silently serving a different vendor than the operator asked for
    would bill the wrong account and change extraction output.
    """
    settings = get_settings()
    requested = (settings.ai.provider or PROVIDER_NOOP).strip().lower()

    if requested in _RETIRED:
        replacement = _RETIRED[requested]
        logger.warning(
            "AI_PROVIDER=%s is retired; using %s. Gemini models are reached through "
            "OpenRouter under their catalogue ids — set AI_MODEL_<JOB>=google/gemini-... "
            "to put a job on Gemini.",
            requested, replacement,
        )
        requested = replacement

    if requested == PROVIDER_OPENROUTER:
        return _build_openrouter(settings)
    if requested == PROVIDER_LOCAL:
        return _build_local(settings)

    if requested != PROVIDER_NOOP:
        logger.warning("Unknown AI_PROVIDER %r; using NoOp provider.", requested)
    return NoOpEnrichmentProvider()


def _build_openrouter(settings: Settings) -> EnrichmentProvider:
    if not settings.ai.openrouter_api_key:
        logger.error("AI_PROVIDER=openrouter but AI_OPENROUTER_API_KEY is unset; using NoOp provider.")
        return NoOpEnrichmentProvider()

    from src.infrastructure.ai.openrouter import OpenRouterOrchestrator

    # No embedder is injected. OpenRouter's catalogue contains no embedding
    # models, so with every job routed through it embeddings are unavailable and
    # deduplication falls back to fuzzy title matching. Restoring them means
    # adding a second provider account, which is the thing this consolidation
    # deliberately gave up.
    logger.info(
        "Using OpenRouter provider (per-job models; single-model override=%s). "
        "Embeddings unavailable; deduplication uses fuzzy matching.",
        settings.ai.openrouter_model or "none",
    )
    return OpenRouterOrchestrator(
        api_key=settings.ai.openrouter_api_key,
        model=settings.ai.openrouter_model,
    )


def _build_local(settings: Settings) -> EnrichmentProvider:
    if not settings.ai.local_base_url:
        logger.error("AI_PROVIDER=local but AI_LOCAL_BASE_URL is unset; using NoOp provider.")
        return NoOpEnrichmentProvider()

    from src.infrastructure.ai.ssot import RESILIENCE
    from src.infrastructure.providers.enrichment_provider import LocalLLMProvider

    logger.info("Using local OpenAI-compatible provider at %s.", settings.ai.local_base_url)
    return LocalLLMProvider(
        base_url=settings.ai.local_base_url,
        model=settings.ai.local_model,
        timeout=RESILIENCE.local_provider_timeout_s,
        max_retries=RESILIENCE.max_retries,
    )


# Module-level singleton. `main.py` imports this; it used to live in the deleted
# ai/client.py alongside the direct-Gemini adapter.
ai_orchestrator: EnrichmentProvider = create_ai_provider()
