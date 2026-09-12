"""
D16: the Postgres observations table silently dropped two fields.

synthesis_service writes groundedness and citation_coverage on every LLM
observation. The SQLite table has both columns; the Postgres table never gained
them, so on the production backend those two numbers were accepted by the port
and discarded by the adapter. Nothing failed -- the evaluator simply learned
from observations whose quality signal was always absent.

The columns are not really the fix. This is: a model field that no backend
persists should fail a test, not degrade a backend. Both adapters are checked
against the domain model here, so the next field added to StrategyObservation
or DomainProfile cannot reach one backend and quietly skip the other.

There is no live Postgres in this environment (see
tests/integration/test_postgres_repos.py), so the Postgres half is asserted
against the DDL and the generated INSERT. Both are where the defect lived.
"""

import re
from datetime import UTC, datetime

import pytest

from src.domain.models import DomainProfile, StrategyObservation
from src.infrastructure.repositories import observation_repository as sqlite_repo
from src.infrastructure.repositories import postgres_observation_repository as pg_repo
from src.infrastructure.repositories.postgres_observation_repository import PostgresObservationRepository


def _ddl_columns(ddl: str) -> set[str]:
    """Column names declared by a CREATE TABLE statement."""
    body = ddl[ddl.index("(") + 1 : ddl.rindex(")")]
    depth = 0
    current = ""
    parts = []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return {part.split()[0] for part in parts if part.split()}


class _CapturingConn:
    def __init__(self):
        self.statements: list[str] = []
        self.params: list[tuple] = []

    async def execute(self, sql, *args):
        self.statements.append(sql)
        self.params.append(args)

    async def fetchrow(self, sql, *args):
        return None

    async def fetch(self, sql, *args):
        return []


OBSERVATION_FIELDS = set(StrategyObservation.model_fields)
PROFILE_FIELDS = set(DomainProfile.model_fields)


@pytest.mark.parametrize(
    "ddl, fields, label",
    [
        (pg_repo.CREATE_OBSERVATIONS_TABLE, OBSERVATION_FIELDS, "postgres/strategy_observations"),
        (sqlite_repo.CREATE_OBSERVATIONS_TABLE, OBSERVATION_FIELDS, "sqlite/strategy_observations"),
        (pg_repo.CREATE_PROFILES_TABLE, PROFILE_FIELDS, "postgres/domain_profiles"),
        (sqlite_repo.CREATE_PROFILES_TABLE, PROFILE_FIELDS, "sqlite/domain_profiles"),
    ],
)
def test_every_model_field_has_a_column(ddl, fields, label):
    missing = fields - _ddl_columns(ddl)
    assert not missing, f"{label} cannot persist: {sorted(missing)}"


def test_the_two_backends_declare_the_same_observation_columns():
    """A column present on one side only is how the two backends drift apart
    without either one failing."""
    assert _ddl_columns(pg_repo.CREATE_OBSERVATIONS_TABLE) == _ddl_columns(
        sqlite_repo.CREATE_OBSERVATIONS_TABLE
    )


def test_the_postgres_migration_adds_the_columns_an_existing_deployment_lacks():
    """CREATE TABLE IF NOT EXISTS is a no-op on a deployment that predates the
    columns, so the DDL alone fixes nothing that is already running."""
    migration = pg_repo.OBSERVATION_MIGRATION_COLUMNS
    added = {column.split()[0] for column in migration}
    assert {"groundedness", "citation_coverage"} <= added


@pytest.mark.asyncio
async def test_the_postgres_insert_writes_every_observation_field():
    """The INSERT used positional VALUES against an unnamed column list, so a
    column added to the table would have been filled with its default and the
    mismatch would never surface as an error."""
    repo = PostgresObservationRepository("postgresql://unused/unused")
    repo._conn = _CapturingConn()

    obs = StrategyObservation(
        observation_id="obs-1", job_id="job-1", domain="example.com", strategy="llm_extract",
        groundedness=0.85, citation_coverage=0.5,
    )
    await repo.create_observation(obs)

    sql = repo._conn.statements[0]
    named = re.search(r"strategy_observations\s*\(([^)]*)\)", sql)
    assert named, f"INSERT must name its columns, not rely on table order: {sql}"
    columns = {c.strip() for c in named.group(1).split(",")}
    assert columns == OBSERVATION_FIELDS

    placeholders = {int(n) for n in re.findall(r"\$(\d+)", sql)}
    assert placeholders == set(range(1, len(repo._conn.params[0]) + 1))


def test_a_postgres_row_reads_back_every_observation_field():
    row = {
        "observation_id": "obs-1", "job_id": "job-1", "domain": "example.com",
        "strategy": "llm_extract", "overlay_id": None, "input_fingerprint": None,
        "valid_record_count": 3, "required_field_completeness": 1.0, "duplicate_rate": 0.0,
        "http_status": 200, "blocked": False, "latency_ms": 12.0, "cost": 0.01,
        "success": True, "groundedness": 0.85, "citation_coverage": 0.5,
        "created_at": datetime(2026, 3, 4, tzinfo=UTC),
    }

    obs = PostgresObservationRepository._row_to_obs(row)

    assert obs.groundedness == 0.85
    assert obs.citation_coverage == 0.5
