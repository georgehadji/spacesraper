# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Configuration Management)
# Role: Centralized configuration using Pydantic Settings.

import os
import warnings
from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """PostgreSQL configuration."""
    model_config = SettingsConfigDict(env_prefix="DB_")
    
    url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+asyncpg://postgres:postgres@localhost:5432/spacescraper"),
        description="Async PostgreSQL connection URL"
    )
    pool_size: int = Field(default=20, ge=1, le=100)
    max_overflow: int = Field(default=10, ge=0, le=50)
    pool_pre_ping: bool = Field(default=True)
    echo: bool = Field(default=False)


def _default_valkey_url() -> str:
    """
    Resolve the broker URL, preferring VALKEY_URL.

    REDIS_URL is still honoured so existing deployments keep working after the
    rename; it is deprecated and will be dropped in a future release.
    """
    url = os.environ.get("VALKEY_URL")
    if url:
        return url
    legacy = os.environ.get("REDIS_URL")
    if legacy:
        warnings.warn(
            "REDIS_URL is deprecated; set VALKEY_URL instead.",
            DeprecationWarning, stacklevel=2,
        )
        return legacy
    return "valkey://localhost:6379/0"


class ValkeySettings(BaseSettings):
    """
    Valkey configuration.

    valkey-py accepts valkey://, valkeys://, redis:// and unix:// URLs, so an
    existing redis:// endpoint keeps working unchanged.
    """
    model_config = SettingsConfigDict(env_prefix="VALKEY_")

    url: str = Field(default_factory=_default_valkey_url)
    socket_timeout: float = Field(default=5.0)
    socket_connect_timeout: float = Field(default=5.0)
    retry_on_timeout: bool = Field(default=True)


# Provider names the AI composition root knows how to act on. "gemini" is
# retired and is listed only so existing .env files keep loading --
# provider_factory._RETIRED maps it onto openrouter with a warning. Typing the
# field turns a typo into a boot-time error; without it AI_PROVIDER=openrouterr
# logged one warning and then ran the whole cluster on NoOp enrichment, which
# looks exactly like AI being switched off on purpose.
AIProviderName = Literal["openrouter", "local", "noop", "gemini"]


class AISettings(BaseSettings):
    """AI/LLM configuration."""
    model_config = SettingsConfigDict(env_prefix="AI_")

    openrouter_api_key: str | None = Field(default=None)
    enabled: bool = Field(default=True)
    timeout: float = Field(default=10.0)
    max_retries: int = Field(default=3)
    embedding_cache_size: int = Field(default=1000)

    # Provider selection (Phase 4) — concrete adapter chosen in the composition
    # root only. 'openrouter' | 'local' | 'noop'. 'gemini' is retired: Gemini is
    # reached through OpenRouter under its catalogue ids (google/gemini-*), so
    # there is one account and one place where model choice and spend are visible.
    provider: AIProviderName = Field(default="openrouter")

    @field_validator("provider", mode="before")
    @classmethod
    def _normalise_provider(cls, value: object) -> object:
        """Keep the case- and whitespace-tolerance provider_factory already had.

        It calls .strip().lower() on the value, so "OpenRouter" and " local "
        worked before this field was typed. A Literal compares the raw string,
        so without this they would now be rejected at boot -- a regression for
        deployments that are configured correctly.

        A blank AI_PROVIDER= is mapped to "noop" for the same reason: the
        factory read it as `settings.ai.provider or PROVIDER_NOOP`, so blanking
        the variable was a working way to switch enrichment off. A Literal
        would otherwise turn that into a boot failure.
        """
        if not isinstance(value, str):
            return value
        return value.strip().lower() or "noop"
    openrouter_model: str | None = Field(
        default=None,
        description=(
            "Optional single-model override forcing every job onto one model. "
            "Leave unset to use the per-job pins in src/infrastructure/ai/ssot.py, "
            "which is where model ids belong. Setting this also disables the "
            "fallback chain and reasoning control, since neither is known to "
            "apply to an arbitrary operator-supplied model."
        ),
    )
    local_base_url: str | None = Field(default=None, description="OpenAI-compatible endpoint, e.g. http://localhost:11434/v1")
    local_model: str | None = Field(default=None, description="Model name as served by the local endpoint.")


class ScraperSettings(BaseSettings):
    """Scraper-specific configuration."""
    model_config = SettingsConfigDict(env_prefix="SCRAPER_")
    
    pool_size: int = Field(default=5, ge=1, le=20)
    headless: bool = Field(default=True)
    timeout: int = Field(default=35000)
    max_depth: int = Field(default=3)
    turbo_mode_enabled: bool = Field(default=True)


class NotificationSettings(BaseSettings):
    """External notification channels."""
    slack_webhook_url: str | None = Field(default=None)
    webhook_secret: str | None = Field(default=None)


# The set of search adapters Discovery knows how to build. This is the SSOT for
# the name; worker_discovery.PROVIDER_FACTORIES must offer exactly these keys,
# and tests/test_worker_discovery.py fails if the two ever drift. Typing the
# field rejects an unrecognised DISCOVERY_SEARCH_PROVIDER at load instead of
# letting it degrade to 'noop', whose empty result set is indistinguishable
# from a query that genuinely matched nothing.
SearchProviderName = Literal["noop", "duckduckgo", "serper", "openrouter"]


class DiscoverySettings(BaseSettings):
    """
    Query-to-URL discovery configuration.
    All defaults are off/deny — Discovery ships dark: flag False + NoOp adapter.
    """
    model_config = SettingsConfigDict(env_prefix="DISCOVERY_")

    enabled: bool = Field(default=False)
    search_provider: SearchProviderName = Field(
        default="noop",
        description=(
            "Which search adapter Discovery uses. Note that 'openrouter' bills "
            "per search request on top of tokens (see "
            "ssot.WEB_SEARCH_PRICE_PER_REQUEST_USD), unlike the others."
        ),
    )
    @field_validator("search_provider", mode="before")
    @classmethod
    def _normalise_search_provider(cls, value: object) -> object:
        """Match AISettings.provider rather than diverging from it.

        A blank DISCOVERY_SEARCH_PROVIDER= is the obvious way to express "no
        search", and casing or a stray space out of a compose file is a
        correctly-intended value. A bare Literal turns each of those into a
        ValidationError at import of this module, which takes down the API and
        every worker -- not just Discovery, which ships dark anyway.
        """
        if not isinstance(value, str):
            return value
        return value.strip().lower() or "noop"

    search_api_key: str | None = Field(default=None)
    allowed_domains: list[str] = Field(default_factory=list, description="Non-empty required to run.")
    denied_domains: list[str] = Field(default_factory=list)
    max_fanout: int = Field(default=25, description="Discovery-specific cap, well below the crawl cap (200).")
    respect_robots: bool = Field(default=True)


class Settings(BaseSettings):
    """
    Spacescraper Master Configuration.
    Loads from environment variables and .env file.
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"  # Allow extra env vars without error
    )
    
    # Environment
    environment: str = Field(default="development")
    debug: bool = Field(default=False)
    
    # Sub-configurations
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    valkey: ValkeySettings = Field(default_factory=ValkeySettings)
    ai: AISettings = Field(default_factory=AISettings)
    scraper: ScraperSettings = Field(default_factory=ScraperSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)

    # Feature flags
    features: dict[str, bool] = Field(default_factory=lambda: {
        "postgres_db": False,
        "turbo_mode": True,
        "ai_enrichment": True,
        "discovery": False,
    })


@lru_cache
def get_settings() -> Settings:
    """
    Singleton settings instance.
    Cached for performance - loads once per process.
    """
    return Settings()


# Global settings instance
settings = get_settings()
