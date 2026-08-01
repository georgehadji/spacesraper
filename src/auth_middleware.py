# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (API Authentication)
# Role: JWT-based API key authentication with tiered rate limiting.

import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional, Dict, Any, Callable
from functools import wraps

from fastapi import HTTPException, Security, Request, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
import jwt
import valkey.asyncio as valkey

from src.config_settings import settings

logger = logging.getLogger("Spacescraper.Auth")

# Fail-safe: prevent DEMO_API_KEY from being active in production
if os.environ.get("DEMO_API_KEY") and os.environ.get("ENVIRONMENT", "development") == "production":
    raise RuntimeError(
        "DEMO_API_KEY is set but ENVIRONMENT=production. "
        "Remove DEMO_API_KEY or set ENVIRONMENT=development."
    )


class ApiTier(Enum):
    """API usage tiers with different rate limits."""
    FREE = "free"           # 100 req/day
    BASIC = "basic"         # 1,000 req/day  
    PRO = "pro"             # 10,000 req/day
    ENTERPRISE = "enterprise"  # 100,000 req/day


# Rate limits per tier (requests per day)
TIER_LIMITS = {
    ApiTier.FREE: 100,
    ApiTier.BASIC: 1000,
    ApiTier.PRO: 10000,
    ApiTier.ENTERPRISE: 100000,
}


class ApiKey(BaseModel):
    """API key metadata."""
    key_id: str
    key_hash: str  # Store hash, not the actual key
    tier: ApiTier
    owner_email: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    is_active: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)


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


class ApiKeyManager:
    """
    Manages API key lifecycle and rate limiting.
    Uses Redis for distributed rate limiting across worker nodes.
    Stores API keys in-memory by default; production deployments should
    replace with a persistent database adapter.
    """
    
    def __init__(self):
        self._redis: Optional[valkey.Redis] = None
        self._keys_by_hash: Dict[str, ApiKey] = {}  # key_hash -> ApiKey
        self.security = HTTPBearer(auto_error=False)
        # Single-node fallback counters: "{key_id}:{yyyymmdd}" -> count
        self._local_counts: Dict[str, int] = {}

    async def initialize(self):
        """
        Initialize the Redis connection.

        The client is verified with a ping: valkey.from_url() connects lazily, so
        without this an unreachable Redis would leave a live-looking client and
        every authenticated request would fail with a ConnectionError instead of
        degrading to the single-node counter.
        """
        try:
            client = valkey.from_url(str(settings.redis.url), decode_responses=True)
            await client.ping()
            self._redis = client
        except Exception as e:
            logger.warning(
                "Rate limiter: Redis unreachable (%s). Falling back to single-node counters.", e
            )
            self._redis = None
    
    async def close(self):
        """Close Redis connection."""
        if self._redis:
            await self._redis.close()
    
    def generate_api_key(self, tier: ApiTier, owner_email: str) -> tuple[str, ApiKey]:
        """
        Generate a new API key.

        Persists the hashed key in memory for validation.
        
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
            created_at=datetime.now(tz=timezone.utc),
            expires_at=None,
        )
        
        # Persist the key metadata
        self._keys_by_hash[key_hash] = api_key
        
        return plain_key, api_key
    
    async def validate_key(self, plain_key: str) -> Optional[ApiKey]:
        """
        Validate an API key and return its metadata.
        Looks up the key hash from the stored keys.
        Returns None for unknown or revoked keys.
        """
        # Hash the provided key
        key_hash = hashlib.sha256(plain_key.encode()).hexdigest()
        
        # Look up from stored keys — unknown keys are rejected
        api_key = self._keys_by_hash.get(key_hash)
        if api_key is None:
            return None
        
        return api_key
    
    def _check_rate_limit_local(self, key_id: str, tier: ApiTier) -> RateLimitInfo:
        """Single-node daily counter used when Redis is unavailable."""
        today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
        counter_key = f"{key_id}:{today}"
        current_count = self._local_counts.get(counter_key, 0)
        limit = TIER_LIMITS[tier]

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
        now = datetime.now(tz=timezone.utc)
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    @classmethod
    def _seconds_until_reset(cls) -> int:
        return int((cls._next_reset() - datetime.now(tz=timezone.utc)).total_seconds())

    async def check_rate_limit(self, key_id: str, tier: ApiTier) -> RateLimitInfo:
        """
        Check and update rate limit for an API key.
        Uses Redis for atomic counter operations across nodes; a Redis outage
        degrades to a single-node counter rather than failing the request.
        """
        if not self._redis:
            return self._check_rate_limit_local(key_id, tier)

        # Daily window key
        today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
        redis_key = f"ratelimit:{key_id}:{today}"
        limit = TIER_LIMITS[tier]

        try:
            current = await self._redis.get(redis_key)
            current_count = int(current) if current else 0

            if current_count >= limit:
                raise RateLimitExceeded(self._seconds_until_reset())

            # Increment counter and expire at midnight
            pipe = self._redis.pipeline()
            pipe.incr(redis_key)
            pipe.expireat(redis_key, int(self._next_reset().timestamp()))
            await pipe.execute()
        except RateLimitExceeded:
            raise
        except Exception as e:
            logger.warning(
                "Rate limiter: Redis error (%s). Falling back to single-node counters.", e
            )
            self._redis = None
            return self._check_rate_limit_local(key_id, tier)

        return RateLimitInfo(
            limit=limit,
            remaining=limit - current_count - 1,
            reset_at=self._next_reset(),
            window="day",
        )


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
            created_at=datetime.now(tz=timezone.utc),
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
    
    if api_key.expires_at and api_key.expires_at < datetime.now(tz=timezone.utc):
        raise HTTPException(
            status_code=403,
            detail="API key expired"
        )
    
    # Check rate limit
    rate_info = await api_key_manager.check_rate_limit(api_key.key_id, api_key.tier)
    
    # Add rate limit headers to response
    request.state.rate_limit = rate_info
    
    return api_key, rate_info


def add_rate_limit_headers(response, rate_info: RateLimitInfo):
    """Add rate limit headers to response."""
    response.headers["X-RateLimit-Limit"] = str(rate_info.limit)
    response.headers["X-RateLimit-Remaining"] = str(rate_info.remaining)
    response.headers["X-RateLimit-Reset"] = str(int(rate_info.reset_at.timestamp()))
    response.headers["X-RateLimit-Window"] = rate_info.window


class ApiKeyGenerator:
    """CLI tool for generating API keys."""
    
    @staticmethod
    def generate(tier: str, email: str) -> str:
        """Generate and print a new API key."""
        tier_enum = ApiTier(tier.lower())
        plain_key, metadata = api_key_manager.generate_api_key(tier_enum, email)
        
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


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 3:
        print("Usage: python auth_middleware.py <tier> <email>")
        print(f"Tiers: {', '.join(t.value for t in ApiTier)}")
        sys.exit(1)
    
    ApiKeyGenerator.generate(sys.argv[1], sys.argv[2])
