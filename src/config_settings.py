# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Configuration Management)
# Role: Centralized configuration using Pydantic Settings.

import os
import warnings
from typing import List, Optional
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, PostgresDsn


class DatabaseSettings(BaseSettings):
    """PostgreSQL configuration."""
    model_config = SettingsConfigDict(env_prefix="DB_")
    
    url: PostgresDsn = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/spacescraper",
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


class KafkaSettings(BaseSettings):
    """Kafka configuration for event-driven architecture."""
    model_config = SettingsConfigDict(env_prefix="KAFKA_")
    
    bootstrap_servers: str = Field(default="localhost:9092")
    client_id: str = Field(default="spacescraper")
    retries: int = Field(default=3)
    retry_backoff_ms: int = Field(default=1000)
    
    # Topics
    jobs_topic: str = Field(default="scraper.jobs")
    raw_data_topic: str = Field(default="scraper.raw_data")
    discovery_topic: str = Field(default="scraper.discovery")
    dlq_topic: str = Field(default="scraper.dlq")


class ObservabilitySettings(BaseSettings):
    """OpenTelemetry and monitoring configuration."""
    model_config = SettingsConfigDict(env_prefix="OTEL_")
    
    service_name: str = Field(default="spacescraper")
    service_version: str = Field(default="2.0.0")
    exporter_endpoint: Optional[str] = Field(default=None)
    
    # Metrics
    metrics_enabled: bool = Field(default=True)
    metrics_port: int = Field(default=9090)
    
    # Tracing
    tracing_enabled: bool = Field(default=True)
    tracing_sampling_rate: float = Field(default=1.0)
    
    # Logging
    log_level: str = Field(default="INFO")
    json_logs: bool = Field(default=True)


class AISettings(BaseSettings):
    """AI/LLM configuration."""
    model_config = SettingsConfigDict(env_prefix="AI_")
    
    gemini_api_key: Optional[str] = Field(default=None)
    enabled: bool = Field(default=True)
    timeout: float = Field(default=10.0)
    max_retries: int = Field(default=3)
    embedding_cache_size: int = Field(default=1000)


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
    slack_webhook_url: Optional[str] = Field(default=None)
    webhook_secret: Optional[str] = Field(default=None)


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
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    ai: AISettings = Field(default_factory=AISettings)
    scraper: ScraperSettings = Field(default_factory=ScraperSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    
    # Feature flags
    features: dict = Field(default_factory=lambda: {
        "kafka_events": False,
        "postgres_db": False,
        "otel_tracing": False,
        "saga_pattern": False,
        "turbo_mode": True,
        "ai_enrichment": True,
    })


@lru_cache()
def get_settings() -> Settings:
    """
    Singleton settings instance.
    Cached for performance - loads once per process.
    """
    return Settings()


# Global settings instance
settings = get_settings()
