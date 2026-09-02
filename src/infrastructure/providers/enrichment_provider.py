# Enrichment provider port and adapters.
# Provides a clean interface for AI/LLM enrichment behind a port.

from abc import ABC, abstractmethod
from typing import Any

from src.domain.prompt_safety import strip_hidden_chars
from src.security.input_sanitizer import redact_pii


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
    set actually used by AIOrchestrator, so callers depend on this port instead
    of importing the concrete orchestrator."""

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


class GeminiEnrichmentProvider(EnrichmentProvider):
    """Gemini-based enrichment provider."""

    def __init__(self, api_key: str | None = None, model: str = "gemini-1.5-flash",
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
            from src.infrastructure.http_client import internal_http
            self._client = internal_http
        return self._client

    async def generate(self, prompt: str, *, timeout: float = 10.0) -> str | None:
        if not self._enabled:
            return None

        client = await self._get_client()
        # SEC-2: key travels as a header, never a URL query parameter.
        url = f"{self.base_url}/{self.model}:generateContent"
        payload = {"contents": [{"parts": [{"text": prompt[:8000]}]}]}

        try:
            response = await client.post(
                url, json=payload, timeout=timeout,
                headers={"x-goog-api-key": self.api_key},
            )
            result = await response.json()
            candidates = result.get("candidates", [])
            if candidates:
                return candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            return None
        except Exception:
            return None

    async def embed(self, text: str) -> list[float] | None:
        # Gemini embeddings are not wired for this adapter; AIOrchestrator covers it.
        return None

    async def generate_overlay(self, html_sample: str) -> dict[str, Any] | None:
        text = await self.generate(
            f"Analyze this HTML and produce a JSON extraction overlay.\n\nHTML:\n{html_sample[:6000]}"
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
        client = await self._get_client()
        # SEC-2: key travels as a header, never a URL query parameter.
        url = f"{self.base_url}/{self.model}:generateContent"

        safe_data = redact_pii(data) if isinstance(data, dict) else data
        safe_data = _sanitize_text_values(safe_data)
        prompt = f"{prompt_hint}\n\nData: {json.dumps(safe_data, indent=2, default=str)}"
        payload = {"contents": [{"parts": [{"text": prompt[:8000]}]}]}

        for attempt in range(self.max_retries):
            try:
                import asyncio
                response = await client.post(
                    url, json=payload, timeout=self.timeout,
                    headers={"x-goog-api-key": self.api_key},
                )
                result = response.json()
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
            except Exception:
                if attempt < self.max_retries - 1:
                    import asyncio
                    await asyncio.sleep(1.0 * (2 ** attempt))
                else:
                    return None

    async def is_available(self) -> bool:
        return self._enabled


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
                    "messages": [{"role": "user", "content": prompt[:8000]}],
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
                json={"model": self.model, "input": text[:2000]},
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
            "Spacescraper extraction. Return ONLY the JSON.\n\nHTML:\n" + html_sample[:6000]
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
                text = await self.generate(prompt[:8000], timeout=self.timeout)
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
