# Tests for ApiKeyManager persistence (W1.2, finding F12).
#
# Previously, API keys lived only in a process-local dict: destroyed on
# restart, invisible to every other worker process, even though
# docker-compose.enterprise.yml runs --workers 4. These tests cover the
# ApiKeyRepository-backed durability, including the exact scenario from the
# plan's G4 exit gate: mint on node A -> use on node B -> restart A -> reuse.

import os

import pytest

from src.auth_middleware import ApiKeyManager
from src.domain.models import ApiTier
from src.infrastructure.repositories.api_key_repository import SqliteApiKeyRepository


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_api_keys.db")


async def _manager(db_path) -> ApiKeyManager:
    """A manager wired to a repository on the given file, with no Valkey (local rate-limit fallback)."""
    manager = ApiKeyManager()
    repo = SqliteApiKeyRepository(db_path=db_path)
    await manager.initialize(repo=repo)
    return manager


@pytest.mark.asyncio
async def test_key_survives_restart_and_is_visible_on_another_node(db_path):
    """The G4 exit gate: mint on node A -> use on node B -> restart A -> reuse."""
    node_a = await _manager(db_path)
    plain_key, minted = await node_a.generate_api_key(ApiTier.PRO, "ops@example.com")

    node_b = await _manager(db_path)
    seen_on_b = await node_b.validate_key(plain_key)
    assert seen_on_b is not None
    assert seen_on_b.key_id == minted.key_id
    await node_b.close()

    await node_a.close()  # simulate node A restarting
    node_a_restarted = await _manager(db_path)
    seen_after_restart = await node_a_restarted.validate_key(plain_key)
    assert seen_after_restart is not None
    assert seen_after_restart.key_id == minted.key_id
    await node_a_restarted.close()

    await node_a.close()


@pytest.mark.asyncio
async def test_unknown_key_is_rejected(db_path):
    manager = await _manager(db_path)
    assert await manager.validate_key("not-a-real-key") is None
    await manager.close()


@pytest.mark.asyncio
async def test_revoked_key_fails_validation_downstream_check(db_path):
    """revoke_key() flips is_active; verify_api_key's is_active check does the rest."""
    manager = await _manager(db_path)
    plain_key, minted = await manager.generate_api_key(ApiTier.BASIC, "user@example.com")

    revoked = await manager.revoke_key(minted.key_id)
    assert revoked is True

    fetched = await manager.validate_key(plain_key)
    assert fetched is not None
    assert fetched.is_active is False
    await manager.close()


@pytest.mark.asyncio
async def test_revoke_unknown_key_id_returns_false(db_path):
    manager = await _manager(db_path)
    assert await manager.revoke_key("key_does_not_exist") is False
    await manager.close()


@pytest.mark.asyncio
async def test_repository_failure_falls_back_to_memory(db_path, monkeypatch):
    """A transient repository outage must degrade to the in-memory copy, not fail the request."""
    manager = await _manager(db_path)
    plain_key, minted = await manager.generate_api_key(ApiTier.FREE, "user@example.com")

    async def broken_get_by_hash(key_hash):
        raise ConnectionError("simulated repository outage")

    monkeypatch.setattr(manager._repo, "get_by_hash", broken_get_by_hash)

    fetched = await manager.validate_key(plain_key)
    assert fetched is not None, "in-memory fallback must serve the key when the repository errors"
    assert fetched.key_id == minted.key_id
    await manager.close()


@pytest.mark.asyncio
async def test_generate_without_initialize_still_works_via_memory_only():
    """A manager that was never initialize()'d (e.g. constructed but not wired to a repo) must not crash."""
    manager = ApiKeyManager()
    plain_key, minted = await manager.generate_api_key(ApiTier.FREE, "user@example.com")
    fetched = await manager.validate_key(plain_key)
    assert fetched is not None
    assert fetched.key_id == minted.key_id
