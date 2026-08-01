# Enrichment provider port and adapters.
# Provides a clean interface for AI/LLM enrichment behind a port.

from typing import Optional, List, Dict, Any, Protocol
from abc import ABC, abstractmethod
from src.security.input_sanitizer import redact_pii


class EnrichmentProvider(ABC):
    """Port for AI/LLM enrichment of extracted data."""

    @abstractmethod
    async def enrich(self, data: Dict[str, Any], prompt_hint: str = "") -> Optional[Dict[str, Any]]:
        """
        Enrich extracted data with AI-powered analysis.
        Returns enriched data or None on failure.
        """
        ...

    @abstractmethod
    async def is_available(self) -> bool:
        """Check if the provider is configured and reachable."""
        ...


class NoOpEnrichmentProvider(EnrichmentProvider):
    """No-op provider that returns data unchanged. Used when AI is disabled."""

    async def enrich(self, data: Dict[str, Any], prompt_hint: str = "") -> Optional[Dict[str, Any]]:
        return data

    async def is_available(self) -> bool:
        return True


class GeminiEnrichmentProvider(EnrichmentProvider):
    """Gemini-based enrichment provider."""

    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-1.5-flash",
                 timeout: float = 10.0, max_retries: int = 3):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._enabled = bool(api_key)
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"
        self._client = None

    async def _get_client(self):
        """Lazy-init of HTTP client."""
        if self._client is None:
            from src.infrastructure.http_client import http_client
            self._client = http_client
        return self._client

    async def enrich(self, data: Dict[str, Any], prompt_hint: str = "") -> Optional[Dict[str, Any]]:
        if not self._enabled:
            return data

        import json
        client = await self._get_client()
        url = f"{self.base_url}/{self.model}:generateContent?key={self.api_key}"

        prompt = f"{prompt_hint}\n\nData: {json.dumps(redact_pii(data) if isinstance(data, dict) else data, indent=2, default=str)}"
        payload = {"contents": [{"parts": [{"text": prompt[:8000]}]}]}

        for attempt in range(self.max_retries):
            try:
                import asyncio
                response = await client.post(url, json=payload, timeout=self.timeout)
                result = await response.json()
                candidates = result.get("candidates", [])
                if candidates:
                    text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    if text.strip():
                        try:
                            clean = text.strip().removeprefix("```json").removesuffix("```").strip()
                            return json.loads(clean)
                        except json.JSONDecodeError:
                            return {"enriched_text": text}
                return None
            except Exception as e:
                if attempt < self.max_retries - 1:
                    import asyncio
                    await asyncio.sleep(1.0 * (2 ** attempt))
                else:
                    return None

    async def is_available(self) -> bool:
        return self._enabled
