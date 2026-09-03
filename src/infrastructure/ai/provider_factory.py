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
PROVIDER_GEMINI = "gemini"
PROVIDER_LOCAL = "local"
PROVIDER_NOOP = "noop"


def create_ai_provider() -> EnrichmentProvider:
    """
    Build the provider named by `AI_PROVIDER`, degrading to NoOp rather than
    raising when its credentials are missing.

    A provider named but unconfigured is a misconfiguration, not a fallback
    opportunity: silently serving Gemini to someone who asked for OpenRouter
    would bill the wrong account and produce different extraction output than
    they configured. The one exception is embeddings, which OpenRouter cannot
    serve at all — see below.
    """
    settings = get_settings()
    requested = (settings.ai.provider or PROVIDER_NOOP).strip().lower()

    if requested == PROVIDER_OPENROUTER:
        return _build_openrouter(settings)
    if requested == PROVIDER_GEMINI:
        return _build_gemini(settings)
    if requested == PROVIDER_LOCAL:
        return _build_local(settings)

    if requested != PROVIDER_NOOP:
        logger.warning(f"Unknown AI_PROVIDER {requested!r}; using NoOp provider.")
    return NoOpEnrichmentProvider()


def _build_openrouter(settings: Settings) -> EnrichmentProvider:
    if not settings.ai.openrouter_api_key:
        logger.error("AI_PROVIDER=openrouter but AI_OPENROUTER_API_KEY is unset; using NoOp provider.")
        return NoOpEnrichmentProvider()

    from src.infrastructure.ai.openrouter import OpenRouterOrchestrator

    # OpenRouter's catalogue has no embedding models, so embeddings are
    # delegated to Gemini when a Gemini key happens to be configured. Without
    # one, dedup clustering falls back to fuzzy title matching — degraded, not
    # broken — so this is a warning rather than an error.
    embedder: EnrichmentProvider | None = None
    if settings.ai.gemini_api_key:
        from src.infrastructure.ai.client import AIOrchestrator

        embedder = AIOrchestrator(api_key=settings.ai.gemini_api_key)
        logger.info("OpenRouter provider: embeddings delegated to Gemini.")
    else:
        logger.warning(
            "OpenRouter provider: no Gemini key configured, so embeddings are "
            "unavailable and deduplication will rely on fuzzy matching alone."
        )

    logger.info(
        f"Using OpenRouter provider (per-job models; "
        f"single-model override={settings.ai.openrouter_model or 'none'})."
    )
    return OpenRouterOrchestrator(
        api_key=settings.ai.openrouter_api_key,
        model=settings.ai.openrouter_model,
        embedder=embedder,
    )


def _build_gemini(settings: Settings) -> EnrichmentProvider:
    if not settings.ai.gemini_api_key:
        logger.error("AI_PROVIDER=gemini but AI_GEMINI_API_KEY is unset; using NoOp provider.")
        return NoOpEnrichmentProvider()

    from src.infrastructure.ai.client import AIOrchestrator

    logger.info("Using Gemini provider.")
    return AIOrchestrator(api_key=settings.ai.gemini_api_key)


def _build_local(settings: Settings) -> EnrichmentProvider:
    if not settings.ai.local_base_url:
        logger.error("AI_PROVIDER=local but AI_LOCAL_BASE_URL is unset; using NoOp provider.")
        return NoOpEnrichmentProvider()

    from src.infrastructure.ai.ssot import RESILIENCE
    from src.infrastructure.providers.enrichment_provider import LocalLLMProvider

    logger.info(f"Using local OpenAI-compatible provider at {settings.ai.local_base_url}.")
    return LocalLLMProvider(
        base_url=settings.ai.local_base_url,
        model=settings.ai.local_model,
        timeout=RESILIENCE.local_provider_timeout_s,
        max_retries=RESILIENCE.max_retries,
    )
