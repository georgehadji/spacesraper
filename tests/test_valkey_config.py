# Guards the Redis -> Valkey migration contract.
# VALKEY_URL is the supported variable; REDIS_URL stays honoured so existing
# deployments keep working, and the offline fallback must be a Valkey client.

import warnings

import pytest

from src.config_settings import Settings


def test_valkey_url_is_preferred(monkeypatch):
    monkeypatch.setenv("VALKEY_URL", "valkey://primary:6379/1")
    monkeypatch.setenv("REDIS_URL", "redis://legacy:6379/9")

    assert Settings().valkey.url == "valkey://primary:6379/1"


def test_legacy_redis_url_still_works_and_warns(monkeypatch):
    monkeypatch.delenv("VALKEY_URL", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://legacy:6379/2")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        url = Settings().valkey.url

    assert url == "redis://legacy:6379/2"
    assert any("REDIS_URL is deprecated" in str(w.message) for w in caught)


def test_default_url_is_valkey_scheme(monkeypatch):
    monkeypatch.delenv("VALKEY_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    assert Settings().valkey.url.startswith("valkey://")


@pytest.mark.parametrize(
    "url",
    ["valkey://localhost:6379", "valkeys://localhost:6379", "redis://localhost:6379"],
)
def test_client_accepts_every_supported_scheme(url):
    """valkey-py speaks valkey://, valkeys:// and redis://, so no endpoint breaks."""
    import valkey.asyncio as valkey

    client = valkey.from_url(url)
    assert client is not None


@pytest.mark.asyncio
async def test_stream_queue_offline_fallback_uses_a_valkey_client():
    from src.infrastructure.queues.stream_queue import ValkeyStreamQueue

    queue = ValkeyStreamQueue()
    await queue._setup_mock()

    assert queue._is_mock
    assert "Valkey" in type(queue._valkey).__name__
    await queue.close()
