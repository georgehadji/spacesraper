# Backend selection for the persistence layer (C8/W5.3).
#
# Defaults to SQLite — the only backend this codebase's test suite actually
# exercises in CI. Opts into Postgres only when PERSISTENCE_BACKEND=postgres
# is set explicitly: DatabaseSettings.url is a PostgresDsn with a default
# value even when DB_URL isn't set, so its mere presence can't signal intent
# to actually use Postgres. Imports are deferred inside each branch so a
# SQLite-only deployment never needs asyncpg importable at module load time.

import os

from src.config_settings import settings
from src.domain.ports import (
    JobRepository,
    ObservationRepository,
    OutboxRepository,
    OverlayRepository,
    RecordRepository,
)


def use_postgres() -> bool:
    return os.environ.get("PERSISTENCE_BACKEND", "sqlite").lower() == "postgres"


def _postgres_dsn() -> str:
    """R-W6.1: DatabaseSettings.url always has a default value
    ('postgres:postgres@localhost'), so an operator who sets
    PERSISTENCE_BACKEND=postgres and forgets DB_URL would otherwise connect
    silently to that default instead of the Postgres they meant to point at.
    Fail at startup instead — this only runs once PERSISTENCE_BACKEND has
    already opted in, so it never affects a SQLite deployment."""
    if not os.environ.get("DB_URL"):
        raise RuntimeError(
            "PERSISTENCE_BACKEND=postgres is set but DB_URL is not. Refusing to "
            "silently connect to the default DSN (postgres:postgres@localhost) — "
            "set DB_URL explicitly."
        )
    return str(settings.database.url)


def make_job_repository() -> JobRepository:
    if use_postgres():
        from src.infrastructure.repositories.postgres_job_repository import PostgresJobRepository
        return PostgresJobRepository(_postgres_dsn())
    from src.infrastructure.repositories.job_repository import SqliteJobRepository
    return SqliteJobRepository()


def make_record_repository() -> RecordRepository:
    if use_postgres():
        from src.infrastructure.repositories.postgres_record_repository import PostgresRecordRepository
        return PostgresRecordRepository(_postgres_dsn())
    from src.infrastructure.repositories.record_repository import SqliteRecordRepository
    return SqliteRecordRepository()


def make_outbox_repository() -> OutboxRepository:
    if use_postgres():
        from src.infrastructure.repositories.postgres_outbox_repository import PostgresOutboxRepository
        return PostgresOutboxRepository(_postgres_dsn())
    from src.infrastructure.repositories.outbox_repository import SqliteOutboxRepository
    return SqliteOutboxRepository()


def make_overlay_repository() -> OverlayRepository:
    if use_postgres():
        from src.infrastructure.repositories.postgres_overlay_repository import PostgresOverlayRepository
        return PostgresOverlayRepository(_postgres_dsn())
    from src.infrastructure.repositories.overlay_repository import SqliteOverlayRepository
    return SqliteOverlayRepository()


def make_observation_repository() -> ObservationRepository:
    if use_postgres():
        from src.infrastructure.repositories.postgres_observation_repository import (
            PostgresObservationRepository,
        )
        return PostgresObservationRepository(_postgres_dsn())
    from src.infrastructure.repositories.observation_repository import SqliteObservationRepository
    return SqliteObservationRepository()
