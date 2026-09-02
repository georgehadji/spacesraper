# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (AI Orchestration)
# Role: Interface for LLM-powered self-healing and data enrichment.

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Any

from src.domain.prompt_safety import sanitize_for_llm, strip_hidden_chars
from src.infrastructure.ai.html_compactor import compact_html_for_prompt
from src.infrastructure.cache import AICache
from src.infrastructure.http_client import internal_http
from src.infrastructure.providers.enrichment_provider import EnrichmentProvider
from src.security.input_sanitizer import redact_pii

logger = logging.getLogger("Spacescraper.AI")


def _sanitize_text_values(value: Any) -> Any:
    """Recursively strip hidden/zero-width chars from string leaves (S5) —
    extracted field values, unlike raw page HTML, carry no structure for
    sanitize_for_llm's hidden-subtree removal to act on, only text."""
    if isinstance(value, str):
        return strip_hidden_chars(value)
    if isinstance(value, dict):
        return {k: _sanitize_text_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_text_values(v) for v in value]
    return value

class AIOrchestrator(EnrichmentProvider):
    """
    Spacescraper AI Node.
    Handles semantic analysis of HTML snippets to fix broken selectors
    and extract data from unstructured sources.
    """
    
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.enabled = bool(self.api_key)
        # Using Google Gemini as default for high-context window scraping
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
        self.embed_url = "https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent"

        # Circuit Breaker variables
        self.failure_count = 0
        self.breaker_threshold = 5
        self.offline_until = 0
        self.cooldown_period = 300  # 5 minutes
        
        # Retry configuration
        self.max_retries = 3
        self.base_delay = 1.0  # seconds

        # AI result cache (two-level: local LRU + Valkey)
        self.cache = AICache(local_maxsize=500)

        # Bulkhead: bounds concurrent in-flight Gemini calls. The circuit
        # breaker only reacts after breaker_threshold failures — it does not
        # prevent an unbounded concurrent burst, which is both a cost
        # exposure and a rate-limit hazard (W1.5).
        self._concurrency_limit = int(os.environ.get("AI_MAX_CONCURRENCY", "10"))
        self._semaphore = asyncio.Semaphore(self._concurrency_limit)

    def _check_circuit(self) -> bool:
        """Verifies if the AI API is allowed to execute."""
        if not self.enabled:
            return False
        if time.time() < self.offline_until:
            return False
        return True
        
    def _record_failure(self, error):
        """Records a failure and triggers the breaker if needed."""
        self.failure_count += 1
        logger.error(f"Spacescraper AI Failure ({self.failure_count}/{self.breaker_threshold}): {error}")
        
        if self.failure_count >= self.breaker_threshold:
            self.offline_until = time.time() + self.cooldown_period
            logger.critical(f"Spacescraper AI CIRCUIT OPEN: Entering OFFLINE_MODE for {self.cooldown_period}s.")

    def _record_success(self):
        """Resets the circuit breaker on successful call."""
        if self.failure_count > 0:
            logger.info("Spacescraper AI CIRCUIT CLOSED: Connection restored.")
        self.failure_count = 0

    async def _call_gemini_api(self, prompt: str, timeout: float, is_embedding: bool = False) -> dict[str, Any] | None:
        """
        Generic Gemini API caller with retry logic.
        
        Args:
            prompt: The text prompt for generation or content for embedding
            timeout: Request timeout in seconds
            is_embedding: Whether this is an embedding request
            
        Returns:
            API response data or None if failed
        """
        if not self._check_circuit():
            return None
            
        # SEC-2: the key travels as a header, never a URL query parameter —
        # query strings land in access/proxy logs, client history, and
        # Referer headers with no redaction path that can reach them.
        url = self.embed_url if is_embedding else self.base_url
        
        if is_embedding:
            payload = {
                "model": "models/text-embedding-004",
                "content": {"parts": [{"text": prompt}]}
            }
        else:
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
        
        async with self._semaphore:
            for attempt in range(self.max_retries):
                try:
                    response = await internal_http.post(
                        url, json=payload, timeout=timeout,
                        headers={"x-goog-api-key": self.api_key},
                    )
                    data = response.json()
                    self._record_success()
                    return data
                except Exception as e:
                    delay = self.base_delay * (2 ** attempt)  # Exponential backoff
                    logger.warning(f"Spacescraper AI API attempt {attempt + 1}/{self.max_retries} failed: {e}. Retrying in {delay}s...")
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(delay)
                    else:
                        self._record_failure(e)
                        return None

    async def heal_selector(self, html_chunk: str, target_description: str) -> str | None:
        """
        Attempts to find a new CSS selector for a broken field.
        """
        prompt = f"""
        Analyze this HTML snippet from a procurement portal. 
        Identify the CSS selector that leads to: {target_description}.
        Return ONLY the CSS selector string, no explanation.

        Snippet:
        {compact_html_for_prompt(sanitize_for_llm(html_chunk), max_chars=4000)}
        """
        
        data = await self._call_gemini_api(prompt, timeout=5.0)
        if data:
            try:
                selector = data['candidates'][0]['content']['parts'][0]['text'].strip()
                logger.info(f"Spacescraper AI: Healed selector found: {selector}")
                return selector
            except (KeyError, IndexError) as e:
                logger.error(f"Spacescraper AI: Invalid response format: {e}")
        return None

    async def generate_overlay(self, html_sample: str) -> dict[str, Any] | None:
        """
        Spacescraper Autograph.
        Analyzes a landing page and generates a declarative extraction overlay.
        Uses two-level cache to avoid redundant API calls.
        """
        # Cache on exactly the text that is sent to the model. Keying on a
        # shorter prefix than the prompt lets two different pages collide.
        sample = compact_html_for_prompt(sanitize_for_llm(html_sample), max_chars=6000)
        cached = await self.cache.get("gemini", "overlay", sample)
        if cached is not None:
            logger.debug("Spacescraper AI: Overlay cache hit, skipping API call.")
            return cached

        prompt = """
        Analyze this HTML from a procurement site.
        Create a JSON 'overlay' for Spacescraper extraction.
        Format must be:
        {
            "entity_type": "Opportunity",
            "container_selector": "CSS_SELECTOR_FOR_ITEM_WRAPPER",
            "field_mappings": {
                "title": "SELECTOR",
                "buyer": "SELECTOR",
                "deadline": "SELECTOR",
                "estimated_budget": "SELECTOR",
                "url": "SELECTOR_WITH_[href]"
            }
        }
        Return ONLY the JSON.

        HTML:
        """ + sample

        data = await self._call_gemini_api(prompt, timeout=10.0)
        if data:
            try:
                raw_json = data['candidates'][0]['content']['parts'][0]['text'].strip()
                clean_json = re.sub(r'```json\n|```', '', raw_json)
                overlay = json.loads(clean_json)
                # Write-through: without this the cache never fills and every
                # request re-bills the same HTML.
                await self.cache.set("gemini", "overlay", sample, overlay)
                return overlay
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                logger.error(f"Spacescraper AI: Failed to parse overlay: {e}")
        return None

    async def enrich_opportunity(self, opportunity_data: dict) -> dict[str, Any] | None:
        """
        LLM Translation & Homogenization.
        Translates fields to English and extracts normalized budget and summaries.
        """
        # Redact PII before sending to external AI, then strip any hidden
        # instruction characters carried through in extracted field text (S5)
        safe_data = redact_pii(opportunity_data) if isinstance(opportunity_data, dict) else opportunity_data
        safe_data = _sanitize_text_values(safe_data)
        prompt = f"""
        Analyze the following procurement opportunity data.
        Task:
        1. Translate the 'title' and 'buyer' into English if they are not.
        2. Create a concise 2-sentence 'summary' of the project.
        3. Convert the 'estimated_budget' to EUR (a normalized float). Use current approx rates. Keep it null if missing or vague.
        Format the output as ONLY raw JSON:
        {{ "title_en": "...", "buyer_en": "...", "summary": "...", "normalized_budget_eur": 1500000.0 }}
        
        Opportunity Data:
        {safe_data}
        """
        
        data = await self._call_gemini_api(prompt, timeout=5.0)
        if data:
            try:
                raw_json = data['candidates'][0]['content']['parts'][0]['text'].strip()
                clean_json = re.sub(r'```json\n|```', '', raw_json)
                return json.loads(clean_json)
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                logger.error(f"Spacescraper AI: Failed to parse enrichment: {e}")
        return None

    async def compute_embedding(self, text: str) -> list[float] | None:
        """
        ML Clustering for Deduplication.
        Creates a numerical vector representation of the text.
        Results are cached by content hash to avoid re-billing identical text.
        """
        if not text:
            return None

        key_text = text[:2000]
        cached = self._get_cached_embedding(key_text)
        if cached is not None:
            return cached

        data = await self._call_gemini_api(key_text, timeout=3.0, is_embedding=True)
        if data:
            embedding = data.get('embedding', {}).get('values')
            if embedding:
                self._cache_embedding(key_text, embedding)
                return embedding
        return None
    
    # Module-level embedding cache: keyed by SHA256, LRU-evicted at MAX_EMBEDDING_CACHE_SIZE
    MAX_EMBEDDING_CACHE_SIZE = 500
    _embedding_cache: OrderedDict[str, list[float]] = OrderedDict()

    def _get_cached_embedding(self, text: str) -> list[float] | None:
        """
        Retrieve a cached embedding by content hash.
        Uses module-level OrderedDict keyed by content hash with LRU eviction.
        """
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        value = self._embedding_cache.get(key)
        if value is not None:
            self._embedding_cache.move_to_end(key)
        return value

    def _cache_embedding(self, text: str, embedding: list[float]) -> None:
        """Store an embedding in the module-level cache with LRU eviction."""
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if key in self._embedding_cache:
            self._embedding_cache.move_to_end(key)
            self._embedding_cache[key] = embedding
        else:
            if len(self._embedding_cache) >= self.MAX_EMBEDDING_CACHE_SIZE:
                self._embedding_cache.popitem(last=False)  # evict oldest
            self._embedding_cache[key] = embedding

    async def compute_embedding_with_cache(self, text: str, cache: dict[str, list[float]]) -> list[float] | None:
        """
        Compute embedding with external cache dictionary for better performance.
        
        Args:
            text: The text to embed
            cache: A dictionary to store/fetch cached embeddings
            
        Returns:
            Embedding vector or None
        """
        if not text:
            return None
            
        cache_key = text[:2000]
        if cache_key in cache:
            return cache[cache_key]
            
        data = await self._call_gemini_api(cache_key, timeout=3.0, is_embedding=True)
        if data:
            embedding = data.get('embedding', {}).get('values')
            if embedding:
                cache[cache_key] = embedding
            return embedding
        return None

    # --- EnrichmentProvider port implementation ---

    async def generate(self, prompt: str, *, timeout: float = 10.0) -> str | None:
        """Free-form text generation, satisfying the EnrichmentProvider port."""
        data = await self._call_gemini_api(prompt, timeout=timeout)
        if data:
            try:
                return data['candidates'][0]['content']['parts'][0]['text'].strip()
            except (KeyError, IndexError):
                return None
        return None

    async def embed(self, text: str) -> list[float] | None:
        """Alias for compute_embedding, satisfying the EnrichmentProvider port."""
        return await self.compute_embedding(text)

    async def enrich(self, data: dict[str, Any], prompt_hint: str = "") -> dict[str, Any] | None:
        """
        Satisfies the EnrichmentProvider port by delegating to enrich_opportunity.
        prompt_hint is accepted for port compatibility but the opportunity-specific
        translation/normalization prompt in enrich_opportunity is used as-is.
        """
        return await self.enrich_opportunity(data)

    async def is_available(self) -> bool:
        """Whether the orchestrator is configured and the circuit breaker is closed."""
        return self._check_circuit()


ai_orchestrator = AIOrchestrator()
