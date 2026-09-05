"""
AI_PROVIDER is a closed set, and the composition root is the only place that
turns it into an adapter.

An unrecognised name used to log one warning and then run the whole cluster on
NoOp enrichment, which is indistinguishable from AI being switched off on
purpose. Degrading to NoOp on *missing credentials* is deliberate and
documented in create_ai_provider; degrading on a *typo* is not.
"""

from typing import get_args

import pytest
from pydantic import ValidationError

from src.config_settings import AIProviderName, AISettings, Settings
from src.infrastructure.ai.provider_factory import (
    PROVIDER_LOCAL,
    PROVIDER_NOOP,
    PROVIDER_OPENROUTER,
    _RETIRED,
    create_ai_provider,
)
from src.infrastructure.providers.enrichment_provider import NoOpEnrichmentProvider


def test_literal_matches_what_the_factory_actually_handles():
    """The drift guard. A name in one and not the other is either a value the
    factory silently NoOps, or a working provider settings refuses to load."""
    assert set(get_args(AIProviderName)) == (
        {PROVIDER_OPENROUTER, PROVIDER_LOCAL, PROVIDER_NOOP} | set(_RETIRED)
    )


def test_unknown_provider_name_is_rejected_at_load():
    with pytest.raises(ValidationError):
        AISettings(provider="openrouterr")


@pytest.mark.parametrize("raw", ["OpenRouter", " openrouter ", "OPENROUTER"])
def test_case_and_whitespace_stay_tolerated(raw):
    """provider_factory called .strip().lower(), so these already worked.
    Typing the field must not regress a correctly configured deployment."""
    assert AISettings(provider=raw).provider == "openrouter"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_provider_still_means_noop(blank):
    """create_ai_provider read `settings.ai.provider or PROVIDER_NOOP`, so
    blanking AI_PROVIDER= was a working way to switch enrichment off. A bare
    Literal would have turned that into a boot failure."""
    assert AISettings(provider=blank).provider == "noop"


def test_retired_name_still_loads():
    """_RETIRED exists so existing .env files keep working. Rejecting the name
    at boot would break exactly the deployments it was written to protect."""
    assert AISettings(provider="gemini").provider == "gemini"


def test_retired_name_resolves_to_its_replacement(monkeypatch):
    settings = Settings(ai=AISettings(provider="gemini", openrouter_api_key="test-key"))
    monkeypatch.setattr(
        "src.infrastructure.ai.provider_factory.get_settings", lambda: settings
    )

    provider = create_ai_provider()

    assert not isinstance(provider, NoOpEnrichmentProvider)
    assert type(provider).__name__ == "OpenRouterOrchestrator"


def test_missing_credentials_still_degrade_to_noop(monkeypatch):
    """The documented behaviour, kept: a named-but-unconfigured provider is a
    deployment state, not a typo, and must not take the cluster down."""
    settings = Settings(ai=AISettings(provider="openrouter", openrouter_api_key=None))
    monkeypatch.setattr(
        "src.infrastructure.ai.provider_factory.get_settings", lambda: settings
    )

    assert isinstance(create_ai_provider(), NoOpEnrichmentProvider)
