# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (API Authentication)
# Role: JWT-based API key authentication with tiered rate limiting.

import hashlib
import hmac
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta

import valkey.asyncio as valkey
from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from src.config_settings import settings
from src.domain.models import ApiKey, ApiTier
from src.domain.ports import ApiKeyRepository
from src.infrastructure.repositories.api_key_repository import SqliteApiKeyRepository

logger = logging.getLogger("Spacescraper.Auth")

# Fail-safe: prevent DEMO_API_KEY from being active in production
if os.environ.get("DEMO_API_KEY") and os.environ.get("ENVIRONMENT", "development") == "production":
    raise RuntimeError(
        "DEMO_API_KEY is set but ENVIRONMENT=production. "
        "Remove DEMO_API_KEY or set ENVIRONMENT=development."
    )


# Rate limits per tier (requests per day)
TIER_LIMITS = {
    ApiTier.FREE: 100,
    ApiTier.BASIC: 1000,
    ApiTier.PRO: 10000,
    ApiTier.ENTERPRISE: 100000,
}


class RateLimitInfo(BaseModel):
    """Rate limit status for response headers."""
    limit: int
    remaining: int
    reset_at: datetime
    window: str = "day"


class AuthenticationError(Exception):
    pass


class RateLimitExceeded(HTTPException):
    def __init__(self, retry_after: int):
        super().__init__(
            status_code=429,
            detail=f"Rate limit exceeded. Retry after {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)}
        )


class _KeyStoreRepoAdapter:
    """Adapts a ValkeyApiKeyStore (save/get_by_hash-as-dict/revoke) to the
    ApiKeyRepository interface ApiKeyManager expects.

    The two stores arrived from different branches with different shapes. This
    keeps both usable rather than forcing a rewrite of either — but note
    get_by_key_id returns None: the Valkey store has no key_id -> key_hash
    index, so revocation by the key_id an operator actually holds is not
    possible through it. Use SqliteApiKeyRepository where revoke_key matters.
    """

    def __init__(self, store):
        self._store = store

    async def initialize(self) -> None:
        # The Valkey store is handed in already connected; nothing to open.
        return None

    async def close(self) -> None:
        # The caller owns the Valkey client's lifecycle, so don't close it here.
        return None

    async def create_key(self, key: ApiKey) -> ApiKey:
        await self._store.save(key.key_hash, key.model_dump(mode="json"))
        return key

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        data = await self._store.get_by_hash(key_hash)
        return ApiKey(**data) if data else None

    async def get_by_key_id(self, key_id: str) -> ApiKey | None:
        return None  # no key_id index on the Valkey store — see class docstring

    async def set_active(self, key_hash: str, is_active: bool) -> ApiKey | None:
        if not is_active:
            await self._store.revoke(key_hash)
        return await self.get_by_hash(key_hash)


class ApiKeyManager:
    """
    Manages API key lifecycle and rate limiting.
    Uses Valkey for distributed rate limiting across worker nodes.

    Keys are persisted via an ApiKeyRepository (SQLite by default) so they
    survive a restart and are visible to every process, not just the one
    that minted them (F12). The in-memory dict is kept as a dual-read/write
    fallback for one release: writes always go to both; reads check the
    repository first and fall back to memory only if the repository is
    unavailable or doesn't have the key (e.g. a key minted before this
    change, or a transient repository outage). Once a release has run with
    the repository as the durable store, the memory path can be dropped.
    """

    def __init__(self, repo: ApiKeyRepository | None = None, key_store=None):
        self._valkey: valkey.Valkey | None = None
        # key_store accepts the Valkey-backed store from the discovery branch;
        # it is adapted to the same ApiKeyRepository interface so both stores
        # work through one code path.
        self._repo = repo if repo is not None else (
            _KeyStoreRepoAdapter(key_store) if key_store is not None else None
        )
        self._keys_by_hash: dict[str, ApiKey] = {}  # key_hash -> ApiKey (fallback)
        self.security = HTTPBearer(auto_error=False)
        # Single-node fallback counters: "{key_id}:{yyyymmdd}" -> count
        self._local_counts: dict[str, int] = {}

    async def initialize(self, repo: ApiKeyRepository | None = None):
        """
        Initialize the Valkey connection and the API key repository.

        The Valkey client is verified with a ping: valkey.from_url() connects
        lazily, so without this an unreachable Valkey would leave a
        live-looking client and every authenticated request would fail with
        a ConnectionError instead of degrading to the single-node counter.
        """
        try:
            client = valkey.from_url(str(settings.valkey.url), decode_responses=True)
            await client.ping()
            self._valkey = client
        except Exception as e:
            logger.warning(
                "Rate limiter: Valkey unreachable (%s). Falling back to single-node counters.", e
            )
            self._valkey = None

        if repo is not None:
            self._repo = repo
        if self._repo is None:
            self._repo = SqliteApiKeyRepository()
        await self._repo.initialize()

    async def close(self):
        """Close the Valkey connection and the API key repository."""
        if self._valkey:
            await self._valkey.close()
        if self._repo:
            await self._repo.close()

    async def generate_api_key(self, tier: ApiTier, owner_email: str) -> tuple[str, ApiKey]:
        """
        Generate a new API key. Persists to the repository (source of truth)
        and to the in-memory fallback.

        Returns:
            Tuple of (plain_key, key_metadata)
            The plain_key should be shown ONCE to the user.
        """
        # Generate cryptographically secure random key (no ss_ prefix)
        plain_key = secrets.token_urlsafe(32)

        # Hash for storage (never store plain key)
        key_hash = hashlib.sha256(plain_key.encode()).hexdigest()
        key_id = f"key_{secrets.token_hex(8)}"

        api_key = ApiKey(
            key_id=key_id,
            key_hash=key_hash,
            tier=tier,
            owner_email=owner_email,
            created_at=datetime.now(tz=UTC),
            expires_at=None,
        )

        if self._repo is not None:
            await self._repo.create_key(api_key)
        self._keys_by_hash[key_hash] = api_key

        return plain_key, api_key

    async def validate_key(self, plain_key: str) -> ApiKey | None:
        """
        Validate an API key and return its metadata.
        Checks the repository first, then the in-memory fallback.
        Returns None for unknown or revoked keys.
        """
        key_hash = hashlib.sha256(plain_key.encode()).hexdigest()

        if self._repo is not None:
            try:
                # The repo is authoritative: a None here means the key is
                # unknown OR revoked, and must NOT fall through to the
                # in-memory cache. That fallthrough kept a revoked key working
                # for the life of the process that minted it, since the cache
                # still held the pre-revocation copy.
                return await self._repo.get_by_hash(key_hash)
            except Exception as e:
                # Only an unreachable repo falls back — degraded, not bypassed.
                logger.warning("API key repository lookup failed (%s); using in-memory fallback.", e)

        return self._keys_by_hash.get(key_hash)

    async def revoke_key(self, key_id: str) -> bool:
        """
        Revoke a key by its key_id (the identifier shown to the operator at
        mint time — never the plain key or its hash). Returns True if a key
        was found and revoked.
        """
        if self._repo is not None:
            existing = await self._repo.get_by_key_id(key_id)
            if existing is not None:
                await self._repo.set_active(existing.key_hash, False)
                self._keys_by_hash.pop(existing.key_hash, None)
                return True

        for key_hash, api_key in self._keys_by_hash.items():
            if api_key.key_id == key_id:
                self._keys_by_hash[key_hash] = api_key.model_copy(update={"is_active": False})
                return True
        return False
    
    def _check_rate_limit_local(self, counter_prefix: str, limit: int) -> RateLimitInfo:
        """Single-node daily counter used when Valkey is unavailable."""
        today = datetime.now(tz=UTC).strftime("%Y%m%d")
        counter_key = f"{counter_prefix}:{today}"
        current_count = self._local_counts.get(counter_key, 0)

        if current_count >= limit:
            raise RateLimitExceeded(self._seconds_until_reset())

        # Drop yesterday's counters so the map cannot grow without bound.
        if len(self._local_counts) > 1024:
            self._local_counts = {
                k: v for k, v in self._local_counts.items() if k.endswith(today)
            }
        self._local_counts[counter_key] = current_count + 1

        return RateLimitInfo(
            limit=limit,
            remaining=limit - current_count - 1,
            reset_at=self._next_reset(),
            window="day",
        )

    @staticmethod
    def _next_reset() -> datetime:
        now = datetime.now(tz=UTC)
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    @classmethod
    def _seconds_until_reset(cls) -> int:
        return int((cls._next_reset() - datetime.now(tz=UTC)).total_seconds())

    async def check_rate_limit(self, key_id: str, tier: ApiTier) -> RateLimitInfo:
        """
        Check and update rate limit for an API key.
        Uses Valkey for atomic counter operations across nodes; a Valkey outage
        degrades to a single-node counter rather than failing the request.
        """
        return await self._check_rate_limit(f"ratelimit:{key_id}", TIER_LIMITS[tier])

    async def check_registration_rate_limit(self, client_ip: str) -> RateLimitInfo:
        """
        Per-IP throttle on POST /auth/register (F11), independent of the
        per-key tier limits above. Registration is gated behind an admin key
        (see verify_admin_key), but this bounds the blast radius of a leaked
        or brute-forced admin key: even a valid admin caller can only mint
        REGISTRATION_IP_LIMIT keys per IP per day.
        """
        return await self._check_rate_limit(f"register_ip:{client_ip}", REGISTRATION_IP_LIMIT)

    async def _check_rate_limit(self, counter_prefix: str, limit: int) -> RateLimitInfo:
        if not self._valkey:
            return self._check_rate_limit_local(counter_prefix, limit)

        # Daily window key
        today = datetime.now(tz=UTC).strftime("%Y%m%d")
        valkey_key = f"{counter_prefix}:{today}"

        try:
            current = await self._valkey.get(valkey_key)
            current_count = int(current) if current else 0

            if current_count >= limit:
                raise RateLimitExceeded(self._seconds_until_reset())

            # Increment counter and expire at midnight
            pipe = self._valkey.pipeline()
            pipe.incr(valkey_key)
            pipe.expireat(valkey_key, int(self._next_reset().timestamp()))
            await pipe.execute()
        except RateLimitExceeded:
            raise
        except Exception as e:
            logger.warning(
                "Rate limiter: Valkey error (%s). Falling back to single-node counters.", e
            )
            self._valkey = None
            return self._check_rate_limit_local(counter_prefix, limit)

        return RateLimitInfo(
            limit=limit,
            remaining=limit - current_count - 1,
            reset_at=self._next_reset(),
            window="day",
        )


# Keys mintable per IP per day via POST /auth/register, regardless of tier (F11).
REGISTRATION_IP_LIMIT = 5

# Global instance
api_key_manager = ApiKeyManager()


async def verify_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Security(api_key_manager.security)
) -> tuple[ApiKey, RateLimitInfo]:
    """
    FastAPI dependency for API key verification.
    
    Usage:
        @app.get("/protected")
        async def protected(user: tuple = Depends(verify_api_key)):
            api_key, rate_info = user
            ...
    """
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="API key required. Include 'Authorization: Bearer <key>' header.",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    # Validate key format
    plain_key = credentials.credentials
    
    # Check for test/demo keys (development only)
    demo_key = os.environ.get("DEMO_API_KEY")
    if settings.environment == "development" and demo_key and plain_key == demo_key:
        api_key = ApiKey(
            key_id="key_demo",
            key_hash="demo_hash",
            tier=ApiTier.PRO,
            owner_email="demo@spacescraper.com",
            created_at=datetime.now(tz=UTC),
            is_active=True
        )
    else:
        api_key = await api_key_manager.validate_key(plain_key)
    
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key"
        )
    
    if not api_key.is_active:
        raise HTTPException(
            status_code=403,
            detail="API key revoked"
        )
    
    if api_key.expires_at and api_key.expires_at < datetime.now(tz=UTC):
        raise HTTPException(
            status_code=403,
            detail="API key expired"
        )
    
    # Check rate limit
    rate_info = await api_key_manager.check_rate_limit(api_key.key_id, api_key.tier)
    
    # Add rate limit headers to response
    request.state.rate_limit = rate_info

    return api_key, rate_info


async def verify_admin_key(
    credentials: HTTPAuthorizationCredentials = Security(api_key_manager.security)
) -> None:
    """
    FastAPI dependency gating admin-only endpoints (currently: POST /auth/register).

    Requires the ADMIN_API_KEY environment variable. If it is unset, every
    request is rejected — there is no "open" fallback state (F11: this
    endpoint previously minted enterprise-tier keys for anonymous callers).
    Comparison is constant-time to avoid a timing side-channel on the key.
    """
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Admin API key required. Include 'Authorization: Bearer <key>' header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    admin_key = os.environ.get("ADMIN_API_KEY", "")
    if not admin_key or not hmac.compare_digest(credentials.credentials, admin_key):
        raise HTTPException(status_code=401, detail="Invalid admin API key")


def add_rate_limit_headers(response, rate_info: RateLimitInfo):
    """Add rate limit headers to response."""
    response.headers["X-RateLimit-Limit"] = str(rate_info.limit)
    response.headers["X-RateLimit-Remaining"] = str(rate_info.remaining)
    response.headers["X-RateLimit-Reset"] = str(int(rate_info.reset_at.timestamp()))
    response.headers["X-RateLimit-Window"] = rate_info.window


class ApiKeyGenerator:
    """
    CLI tool for generating and revoking API keys. Since POST /auth/register
    is admin-gated (F11), this CLI — run on the host with filesystem access
    to the key store — is the other supported way to mint or revoke a key.
    """

    @staticmethod
    async def generate(tier: str, email: str) -> str:
        """Generate and print a new API key. Persists via api_key_manager's repository."""
        tier_enum = ApiTier(tier.lower())
        await api_key_manager.initialize()
        try:
            plain_key, metadata = await api_key_manager.generate_api_key(tier_enum, email)
        finally:
            await api_key_manager.close()

        print("=" * 60)
        print("NEW API KEY GENERATED")
        print("=" * 60)
        print(f"Key:        {plain_key}")
        print(f"Tier:       {metadata.tier.value}")
        print(f"Owner:      {metadata.owner_email}")
        print(f"Created:    {metadata.created_at}")
        print(f"Rate Limit: {TIER_LIMITS[tier_enum]} requests/day")
        print("=" * 60)
        print("⚠️  SAVE THIS KEY NOW - it will not be shown again!")
        print("=" * 60)

        return plain_key

    @staticmethod
    async def revoke(key_id: str) -> bool:
        """Revoke a key by its key_id (F12: no revocation path previously existed)."""
        await api_key_manager.initialize()
        try:
            revoked = await api_key_manager.revoke_key(key_id)
        finally:
            await api_key_manager.close()

        print(f"Key {key_id}: {'REVOKED' if revoked else 'NOT FOUND'}")
        return revoked


if __name__ == "__main__":
    import asyncio
    import sys

    def _usage() -> None:
        print("Usage: python auth_middleware.py generate <tier> <email>")
        print("       python auth_middleware.py revoke <key_id>")
        print(f"Tiers: {', '.join(t.value for t in ApiTier)}")

    if len(sys.argv) < 2:
        _usage()
        sys.exit(1)

    command = sys.argv[1]
    if command == "generate" and len(sys.argv) == 4:
        asyncio.run(ApiKeyGenerator.generate(sys.argv[2], sys.argv[3]))
    elif command == "revoke" and len(sys.argv) == 3:
        asyncio.run(ApiKeyGenerator.revoke(sys.argv[2]))
    else:
        _usage()
        sys.exit(1)
