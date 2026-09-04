# Enrichment provider port and adapters.
# Provides a clean interface for AI/LLM enrichment behind a port.

from abc import ABC, abstractmethod
from typing import Any

from src.domain.prompt_safety import strip_hidden_chars
from src.security.input_sanitizer import redact_pii


def _overlay_budget() -> int:
    """Prompt character budget for overlay generation, from the AI SSOT."""
    from src.infrastructure.ai.ssot import AIJob, profile_for

    return profile_for(AIJob.OVERLAY).max_prompt_chars


def _generate_budget() -> int:
    """Prompt character budget for free-form generation, from the AI SSOT."""
    from src.infrastructure.ai.ssot import AIJob, profile_for

    return profile_for(AIJob.GENERATE).max_prompt_chars


def _embedding_key_chars() -> int:
    """Character budget for embedding input, from the AI SSOT."""
    from src.infrastructure.ai.ssot import CACHE

    return CACHE.embedding_key_chars


def _sanitize_text_values(value: Any) -> Any:
    """Recursively strip hidden/zero-width chars from string leaves (S5)."""
    if isinstance(value, str):
        return strip_hidden_chars(value)
    if isinstance(value, dict):
        return {k: _sanitize_text_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_text_values(v) for v in value]
    return value


class EnrichmentProvider(ABC):
    """Port for AI/LLM enrichment of extracted data. Widened to the capability
    set actually used by the OpenRouter adapter, so callers depend on this port
    instead of importing a concrete orchestrator."""

    @abstractmethod
    async def generate(self, prompt: str, *, timeout: float = 10.0) -> str | None:
        """Free-form text generation from a prompt. Returns raw text or None on failure."""
        ...

    @abstractmethod
    async def embed(self, text: str) -> list[float] | None:
        """Compute an embedding vector for text. Returns None on failure."""
        ...

    @abstractmethod
    async def generate_overlay(self, html_sample: str) -> dict[str, Any] | None:
        """Analyze an HTML sample and generate a declarative extraction overlay."""
        ...

    @abstractmethod
    async def enrich(self, data: dict[str, Any], prompt_hint: str = "") -> dict[str, Any] | None:
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

    async def generate(self, prompt: str, *, timeout: float = 10.0) -> str | None:
        return None

    async def embed(self, text: str) -> list[float] | None:
        return None

    async def generate_overlay(self, html_sample: str) -> dict[str, Any] | None:
        return None


    async def enrich(self, data: dict[str, Any], prompt_hint: str = "") -> dict[str, Any] | None:
        return data

    async def is_available(self) -> bool:
        return True


# GeminiEnrichmentProvider was removed: Gemini is now reached only through
# OpenRouter, under its catalogue ids (google/gemini-*). Keeping a second,
# direct generativelanguage.googleapis.com adapter would mean a second set of
# credentials and a second place model choice and spend could drift.


class LocalLLMProvider(EnrichmentProvider):
    """
    Adapter for a local model served behind an OpenAI-compatible HTTP endpoint
    (Ollama, llama.cpp server, vLLM). Deliberately does NOT import torch or
    transformers: the model runs in a separate process, so BrowserContextPool's
    memory assumptions and pool_size arithmetic are unaffected, and workers
    stay horizontally scalable. The adapter is a thin http_client call
    implementing the same port as every other provider, so the circuit
    breaker, retry-with-backoff, and AICache patterns elsewhere are untouched.

    Security: the local endpoint is typically on a private address. This
    adapter uses its own scoped client (create_scoped_client) that exempts
    ONLY the configured host from the SSRF guard's private-IP check — never
    the shared HttpClient singleton, and never a general relaxation.
    """

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 timeout: float = 30.0, max_retries: int = 3):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or "local-model"
        self.timeout = timeout
        self.max_retries = max_retries
        self._enabled = bool(self.base_url and model)
        self._client = None
        self._allowed_host: str | None = None
        if self.base_url:
            from urllib.parse import urlparse
            self._allowed_host = urlparse(self.base_url).hostname

    async def _get_client(self):
        if self._client is None:
            from src.infrastructure.http_client import create_scoped_client
            hosts = {self._allowed_host} if self._allowed_host else set()
            self._client = create_scoped_client(allowed_private_hosts=hosts, timeout=self.timeout)
        return self._client

    async def generate(self, prompt: str, *, timeout: float = 10.0) -> str | None:
        if not self._enabled:
            return None

        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt[:_generate_budget()]}],
                },
                timeout=timeout,
            )
            data = response.json()
            choices = data.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
            return None
        except Exception:
            return None

    async def embed(self, text: str) -> list[float] | None:
        if not self._enabled or not text:
            return None

        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.base_url}/embeddings",
                json={"model": self.model, "input": text[:_embedding_key_chars()]},
                timeout=self.timeout,
            )
            data = response.json()
            items = data.get("data", [])
            return items[0].get("embedding") if items else None
        except Exception:
            return None

    async def generate_overlay(self, html_sample: str) -> dict[str, Any] | None:
        text = await self.generate(
            "Analyze this HTML from a procurement site. Create a JSON 'overlay' for "
            "Spacescraper extraction. Return ONLY the JSON.\n\nHTML:\n" + html_sample[:_overlay_budget()]
        )
        if not text:
            return None
        import json
        try:
            clean = text.strip().removeprefix("```json").removesuffix("```").strip()
            return json.loads(clean)
        except json.JSONDecodeError:
            return None

    async def enrich(self, data: dict[str, Any], prompt_hint: str = "") -> dict[str, Any] | None:
        if not self._enabled:
            return data

        import json
        prompt = f"{prompt_hint}\n\nData: {json.dumps(data, indent=2, default=str)}"

        for attempt in range(self.max_retries):
            try:
                text = await self.generate(prompt[:_generate_budget()], timeout=self.timeout)
                if text and text.strip():
                    try:
                        clean = text.strip().removeprefix("```json").removesuffix("```").strip()
                        return json.loads(clean)
                    except json.JSONDecodeError:
                        return {"enriched_text": text}
                return None
            except Exception:
                if attempt < self.max_retries - 1:
                    import asyncio
                    await asyncio.sleep(1.0 * (2 ** attempt))
                else:
                    return None

    async def is_available(self) -> bool:
        return self._enabled

    async def close(self):
        """Release the scoped client. Call on shutdown if this adapter was used."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
