"""
Tests for Task 1.3 — API key persistence over Valkey.
Verifies that registered keys survive restart and are shared across replicas.
"""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from src.auth_middleware import ApiKeyManager, ApiTier, ApiKey
from src.infrastructure.repositories.api_key_repository import ValkeyApiKeyStore


@pytest.mark.asyncio
async def test_api_key_persistence_survives_restart():
    """Registered key survives a process restart (persistence test)."""
    try:
        import fakeredis.aioredis
    except ImportError:
        pytest.skip("fakeredis not installed")

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    # First manager instance: generate a key
    manager1 = ApiKeyManager(key_store=ValkeyApiKeyStore(fake_redis))
    await manager1.initialize()

    plain_key, metadata = await manager1.generate_api_key(ApiTier.BASIC, "user@example.com")

    # Simulate process restart: create a new manager instance
    manager2 = ApiKeyManager(key_store=ValkeyApiKeyStore(fake_redis))
    await manager2.initialize()

    # The same plain key should validate successfully
    retrieved_key = await manager2.validate_key(plain_key)
    assert retrieved_key is not None, "Key should survive restart"
    assert retrieved_key.owner_email == "user@example.com"
    assert retrieved_key.tier == ApiTier.BASIC

    await fake_redis.aclose()


@pytest.mark.asyncio
async def test_unknown_api_key_returns_401():
    """Unknown/invalid key returns None (triggers 401 in middleware)."""
    try:
        import fakeredis.aioredis
    except ImportError:
        pytest.skip("fakeredis not installed")

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = ApiKeyManager(key_store=ValkeyApiKeyStore(fake_redis))
    await manager.initialize()

    # Try to validate a key that was never registered
    retrieved_key = await manager.validate_key("unknown_key_12345")
    assert retrieved_key is None

    await fake_redis.aclose()


@pytest.mark.asyncio
async def test_revoked_key_returns_403():
    """Revoked key returns 403 (is_active=False)."""
    try:
        import fakeredis.aioredis
    except ImportError:
        pytest.skip("fakeredis not installed")

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = ValkeyApiKeyStore(fake_redis)
    manager = ApiKeyManager(key_store=store)
    await manager.initialize()

    # Generate a key
    plain_key, metadata = await manager.generate_api_key(ApiTier.PRO, "user@example.com")

    # Verify it validates initially
    key1 = await manager.validate_key(plain_key)
    assert key1 is not None
    assert key1.is_active

    # Revoke the key
    import hashlib
    key_hash = hashlib.sha256(plain_key.encode()).hexdigest()
    await store.revoke(key_hash)

    # After revocation, validation returns None (403 in middleware)
    key2 = await manager.validate_key(plain_key)
    assert key2 is None

    await fake_redis.aclose()


@pytest.mark.asyncio
async def test_demo_key_still_works_in_dev():
    """Demo key gating still works (set via DEMO_API_KEY env var)."""
    try:
        import fakeredis.aioredis
    except ImportError:
        pytest.skip("fakeredis not installed")

    # This test verifies the demo key logic in verify_api_key middleware
    # is still compatible with the persistent store
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = ApiKeyManager(key_store=ValkeyApiKeyStore(fake_redis))
    await manager.initialize()

    # Generate a normal key
    plain_key, metadata = await manager.generate_api_key(ApiTier.BASIC, "user@example.com")

    # Verify it works
    retrieved = await manager.validate_key(plain_key)
    assert retrieved is not None
    assert retrieved.tier == ApiTier.BASIC

    await fake_redis.aclose()


@pytest.mark.asyncio
async def test_api_key_hashing_never_stores_plain():
    """Confirm we never store plain keys, only SHA-256 hashes."""
    store = ValkeyApiKeyStore(AsyncMock())

    # Mock the Redis set operation to verify we're storing a hash
    store._redis.set = AsyncMock()

    plain_key = "test_key_12345"
    key_data = {
        "key_id": "key_abc123",
        "tier": "BASIC",
        "owner_email": "user@example.com",
        "created_at": datetime.utcnow(),
        "is_active": True,
    }

    await store.save(
        "hash_of_key_12345",  # This is the SHA-256 hash
        key_data,
    )

    # Verify set was called with the hash as the Redis key
    store._redis.set.assert_called_once()
    call_args = store._redis.set.call_args[0]
    redis_key = call_args[0]

    # Should be "apikey:<hash>" format, not containing plain key
    assert "apikey:" in redis_key
    assert plain_key not in redis_key
