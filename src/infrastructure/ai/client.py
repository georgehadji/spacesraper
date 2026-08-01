# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (AI Orchestration)
# Role: Interface for LLM-powered self-healing and data enrichment.

import asyncio
import json
import logging
import os
import re
import time
from typing import Optional, List, Dict, Any
from collections import OrderedDict
from src.infrastructure.http_client import http_client
from src.infrastructure.cache import AICache

logger = logging.getLogger("Spacescraper.AI")

class AIOrchestrator:
    """
    Spacescraper AI Node.
    Handles semantic analysis of HTML snippets to fix broken selectors 
    and extract data from unstructured sources.
    """
    
    def __init__(self, api_key: Optional[str] = None):
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

    async def _call_gemini_api(self, prompt: str, timeout: float, is_embedding: bool = False) -> Optional[Dict[str, Any]]:
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
            
        url = self.embed_url if is_embedding else self.base_url
        url = f"{url}?key={self.api_key}"
        
        if is_embedding:
            payload = {
                "model": "models/text-embedding-004",
                "content": {"parts": [{"text": prompt}]}
            }
        else:
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
        
        for attempt in range(self.max_retries):
            try:
                response = await http_client.post(url, json=payload, timeout=timeout)
                data = await response.json()
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

    async def heal_selector(self, html_chunk: str, target_description: str) -> Optional[str]:
        """
        Attempts to find a new CSS selector for a broken field.
        """
        prompt = f"""
        Analyze this HTML snippet from a procurement portal. 
        Identify the CSS selector that leads to: {target_description}.
        Return ONLY the CSS selector string, no explanation.
        
        Snippet:
        {html_chunk[:4000]}
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

    async def generate_overlay(self, html_sample: str) -> Optional[Dict[str, Any]]:
        """
        Spacescraper Autograph.
        Analyzes a landing page and generates a declarative extraction overlay.
        Uses two-level cache to avoid redundant API calls.
        """
        # Check cache first
        cached = await self.cache.get("gemini", "overlay", html_sample[:2000])
        if cached is not None:
            return cached

        prompt = """
        Analyze this HTML from a procurement site. 
        Create a JSON 'overlay' for Spacescraper extraction.
        Format must be: 
        {
            "entity_type": "Opportunity",
            "container": "CSS_SELECTOR_FOR_ITEM_WRAPPER",
            "mapping": {
                "title": "SELECTOR",
                "buyer": "SELECTOR",
                "deadline": "SELECTOR",
                "estimated_budget": "SELECTOR",
                "url": "SELECTOR_WITH_[href]"
            }
        }
        Return ONLY the JSON.
        
        HTML:
        """ + html_sample[:6000]
        
        data = await self._call_gemini_api(prompt, timeout=10.0)
        if data:
            try:
                raw_json = data['candidates'][0]['content']['parts'][0]['text'].strip()
                clean_json = re.sub(r'```json\n|```', '', raw_json)
                return json.loads(clean_json)
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                logger.error(f"Spacescraper AI: Failed to parse overlay: {e}")
        return None

    async def enrich_opportunity(self, opportunity_data: dict) -> Optional[Dict[str, Any]]:
        """
        LLM Translation & Homogenization.
        Translates fields to English and extracts normalized budget and summaries.
        """
        # Redact PII before sending to external AI
        safe_data = redact_pii(opportunity_data) if isinstance(opportunity_data, dict) else opportunity_data
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

    async def compute_embedding(self, text: str) -> Optional[List[float]]:
        """
        ML Clustering for Deduplication.
        Creates a numerical vector representation of the text.
        Results are cached for 1000 most recent texts to improve performance.
        """
        if not text:
            return None
        return await self._get_cached_embedding(text[:2000])
    
    # Module-level embedding cache: keyed by SHA256, LRU-evicted at MAX_EMBEDDING_CACHE_SIZE
    MAX_EMBEDDING_CACHE_SIZE = 500
    _embedding_cache: OrderedDict[str, List[float]] = OrderedDict()

    def _get_cached_embedding(self, text: str) -> Optional[List[float]]:
        """
        Retrieve a cached embedding by content hash.
        Uses module-level OrderedDict keyed by content hash with LRU eviction.
        """
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        value = self._embedding_cache.get(key)
        if value is not None:
            self._embedding_cache.move_to_end(key)
        return value

    def _cache_embedding(self, text: str, embedding: List[float]) -> None:
        """Store an embedding in the module-level cache with LRU eviction."""
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if key in self._embedding_cache:
            self._embedding_cache.move_to_end(key)
            self._embedding_cache[key] = embedding
        else:
            if len(self._embedding_cache) >= self.MAX_EMBEDDING_CACHE_SIZE:
                self._embedding_cache.popitem(last=False)  # evict oldest
            self._embedding_cache[key] = embedding

    async def compute_embedding_with_cache(self, text: str, cache: Dict[str, List[float]]) -> Optional[List[float]]:
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

ai_orchestrator = AIOrchestrator()
