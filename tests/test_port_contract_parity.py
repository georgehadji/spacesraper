# R-W2.4 (docs/plans/2026-09-01-review-remediation.md): a Protocol in
# src/domain/ports.py is never checked against its adapters at import time —
# nothing stops a method from being declared on a port and left unimplemented
# on one or both concrete repositories. That gap is exactly how
# purge_expired_jobs and soft_delete_job went missing on every backend while
# JobReaper called them in production daily (R2/R7). This test makes that
# class of gap fail CI instead of failing silently into a log line.
#
# Two checks per (port, adapter) pair:
#   1. every method the port declares exists on the adapter.
#   2. every parameter name the port declares is accepted by the adapter, and
#      the adapter introduces no *required* parameter the port doesn't know
#      about (extra *optional* adapter params — e.g. a future conn= plumbed
#      in by R-W1 — are fine; that's how a real unit-of-work parameter gets
#      added to one call path without becoming mandatory everywhere else).
#
# This intentionally does not compare parameter order, defaults, or return
# types — that would make the test brittle against harmless adapter-side
# variation without catching a materially different class of bug than the
# two checks above already do.

import inspect

import pytest

from src.domain import ports
from src.infrastructure.repositories.job_repository import SqliteJobRepository
from src.infrastructure.repositories.observation_repository import SqliteObservationRepository
from src.infrastructure.repositories.outbox_repository import SqliteOutboxRepository
from src.infrastructure.repositories.overlay_repository import SqliteOverlayRepository
from src.infrastructure.repositories.postgres_job_repository import PostgresJobRepository
from src.infrastructure.repositories.postgres_observation_repository import PostgresObservationRepository
from src.infrastructure.repositories.postgres_outbox_repository import PostgresOutboxRepository
from src.infrastructure.repositories.postgres_overlay_repository import PostgresOverlayRepository
from src.infrastructure.repositories.postgres_record_repository import PostgresRecordRepository
from src.infrastructure.repositories.record_repository import SqliteRecordRepository

# The five ports factory.py actually switches between SQLite and Postgres.
# FetcherPort/RobotsPort/ProxyProviderPort/ApiKeyRepository/MessageBus have
# different, non-parallel adapter sets and aren't in scope here.
PORT_ADAPTER_PAIRS = [
    (ports.JobRepository, SqliteJobRepository, PostgresJobRepository),
    (ports.RecordRepository, SqliteRecordRepository, PostgresRecordRepository),
    (ports.OutboxRepository, SqliteOutboxRepository, PostgresOutboxRepository),
    (ports.OverlayRepository, SqliteOverlayRepository, PostgresOverlayRepository),
    (ports.ObservationRepository, SqliteObservationRepository, PostgresObservationRepository),
]


def _protocol_method_names(proto: type) -> list[str]:
    return [
        name
        for name, member in vars(proto).items()
        if not name.startswith("_") and callable(member)
    ]


def _param_names(func) -> set[str]:
    return {p for p in inspect.signature(func).parameters if p != "self"}


def _required_param_names(func) -> set[str]:
    sig = inspect.signature(func)
    return {
        name
        for name, param in sig.parameters.items()
        if name != "self"
        and param.default is inspect.Parameter.empty
        and param.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    }


CASES = [
    (port, adapter)
    for port, sqlite_cls, pg_cls in PORT_ADAPTER_PAIRS
    for adapter in (sqlite_cls, pg_cls)
]


@pytest.mark.parametrize("port,adapter", CASES, ids=[f"{p.__name__}::{a.__name__}" for p, a in CASES])
def test_adapter_implements_every_port_method(port: type, adapter: type) -> None:
    for method_name in _protocol_method_names(port):
        assert hasattr(adapter, method_name), (
            f"{adapter.__name__} does not implement {port.__name__}.{method_name}, "
            f"declared at {port.__module__}. A caller typed against the port will "
            f"raise AttributeError at runtime on this backend."
        )


@pytest.mark.parametrize("port,adapter", CASES, ids=[f"{p.__name__}::{a.__name__}" for p, a in CASES])
def test_adapter_signature_covers_port_parameters(port: type, adapter: type) -> None:
    for method_name in _protocol_method_names(port):
        if not hasattr(adapter, method_name):
            continue  # reported by test_adapter_implements_every_port_method
        proto_params = _param_names(getattr(port, method_name))
        adapter_method = getattr(adapter, method_name)
        adapter_params = _param_names(adapter_method)
        adapter_required = _required_param_names(adapter_method)

        missing_on_adapter = proto_params - adapter_params
        assert not missing_on_adapter, (
            f"{adapter.__name__}.{method_name} doesn't accept parameter(s) "
            f"{missing_on_adapter} declared on {port.__name__}.{method_name}."
        )

        surplus_required = adapter_required - proto_params
        assert not surplus_required, (
            f"{adapter.__name__}.{method_name} requires parameter(s) {surplus_required} "
            f"that {port.__name__}.{method_name} never declares — a caller written "
            f"against the port's documented signature will raise TypeError on this "
            f"backend. Give it a default, or add it to the port if every backend needs it."
        )
