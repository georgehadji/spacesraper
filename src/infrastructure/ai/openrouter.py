# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (OpenRouter adapter)
# Role: EnrichmentProvider backed by OpenRouter, one pinned model per job.
#
# Every model id, endpoint, timeout, temperature, prompt budget and resilience
# constant in this file comes from src/infrastructure/ai/ssot.py. Nothing is
# hardcoded here.
#
# Prompt layout is load-bearing. Both pinned primaries cache prompts
# automatically (DeepSeek reads cached input at 0.1x, Z.AI at ~0.2x) and
# providers cache on a prefix match, so every prompt below puts its fixed
# instructions and output schema FIRST and the variable page HTML or record
# fields LAST. Interpolating variable data nearer the top silently stops the
# cache matching: nothing breaks, no test fails, the bill just goes up.
# See ssot.PROMPT_CACHE_NOTE.

import asyncio
import json
import logging
import re
import time
from typing import Any

from src.domain.prompt_safety import sanitize_for_llm, strip_hidden_chars
from src.infrastructure.ai.html_compactor import compact_html_for_prompt
from src.infrastructure.ai.ssot import (
    CACHE,
    ENDPOINTS,
    RESILIENCE,
    AIJob,
    JobProfile,
    profile_for,
)
from src.infrastructure.cache import AICache
from src.infrastructure.http_client import internal_http
from src.infrastructure.providers.enrichment_provider import EnrichmentProvider
from src.security.input_sanitizer import redact_pii

logger = logging.getLogger("Spacescraper.OpenRouter")

_JSON_FENCE = re.compile(r"```(?:json)?\s*|\s*```")


def _sanitize_text_values(value: Any) -> Any:
    """Recursively strip hidden/zero-width chars from string leaves (S5)."""
    if isinstance(value, str):
        return strip_hidden_chars(value)
    if isinstance(value, dict):
        return {k: _sanitize_text_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_text_values(v) for v in value]
    return value


class OpenRouterOrchestrator(EnrichmentProvider):
    """
    OpenRouter-backed enrichment provider.

    Dispatches each job to its own pinned model (see ssot.JOB_PROFILES): the
    schema-generation job gets a stronger
    model than the per-record enrichment job, which in turn gets a stronger
    model than selector healing. That split is the whole point of routing
    through OpenRouter.

    Embeddings are NOT served here and cannot be: the OpenRouter catalogue
    contains no embedding models at all, so `embed` always returns None and
    deduplication falls back to fuzzy title matching.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ):
        self.api_key = api_key
        self.enabled = bool(self.api_key)
        # Optional single-model pin. When set it overrides every job's model —
        # an escape hatch for operators, not the default path.
        self._model_override = model

        self.failure_count = 0
        self.offline_until = 0.0

        self.cache = AICache(local_maxsize=CACHE.result_cache_maxsize)
        self._semaphore = asyncio.Semaphore(RESILIENCE.max_concurrency)

    # -- resilience -------------------------------------------------------

    def _check_circuit(self) -> bool:
        if not self.enabled:
            return False
        return time.time() >= self.offline_until

    def _record_failure(self, error: Exception) -> None:
        self.failure_count += 1
        logger.error(
            f"OpenRouter failure ({self.failure_count}/{RESILIENCE.breaker_threshold}): {error}"
        )
        if self.failure_count >= RESILIENCE.breaker_threshold:
            self.offline_until = time.time() + RESILIENCE.cooldown_period_s
            logger.critical(
                f"OpenRouter CIRCUIT OPEN: offline for {RESILIENCE.cooldown_period_s}s"
            )

    def _record_success(self) -> None:
        if self.failure_count > 0:
            logger.info("OpenRouter CIRCUIT CLOSED: connection restored")
        self.failure_count = 0

    # -- transport --------------------------------------------------------

    def _model_for(self, profile: JobProfile) -> str:
        return self._model_override or profile.model.id

    def _build_payload(self, profile: JobProfile, prompt: str) -> dict[str, Any]:
        """Assemble the chat-completions body for `profile`.

        Three parameters are conditional rather than always-on, each for a
        reason that only shows up in the catalogue metadata:

        * `models` — the fallback chain. OpenRouter tries the entries in order
          on context-length errors, moderation blocks, rate limits and provider
          downtime, and bills whichever model actually answers.
        * `temperature` — 81 of 424 catalogue models reject it outright (OpenAI
          reasoning models are fixed at 1). Sending it to those is a 400, so it
          goes out only when every model in the chain accepts it.
        * `reasoning` — reasoning tokens bill as output tokens, and most of the
          pinned models reason by default. Each job states what it wants.
        """
        payload: dict[str, Any] = {
            "model": self._model_for(profile),
            "messages": [{"role": "user", "content": prompt}],
        }

        if not self._model_override and len(profile.model_chain) > 1:
            payload["models"] = list(profile.model_chain)

        if profile.temperature_allowed or self._model_override:
            payload["temperature"] = profile.temperature

        # Ask for a JSON object only when the pinned model advertises support;
        # sending response_format to a model without it is a 400, not a
        # graceful degradation.
        if profile.expects_json and profile.model.structured_outputs and not self._model_override:
            payload["response_format"] = {"type": "json_object"}

        reasoning = _reasoning_block(profile)
        if reasoning is not None and not self._model_override:
            payload["reasoning"] = reasoning

        return payload

    async def _call(self, profile: JobProfile, prompt: str) -> str | None:
        """Run one chat completion for `profile`, returning the raw text."""
        if not self._check_circuit():
            return None

        payload = self._build_payload(profile, prompt)

        async with self._semaphore:
            for attempt in range(RESILIENCE.max_retries):
                try:
                    response = await internal_http.post(
                        ENDPOINTS.openrouter_chat,
                        json=payload,
                        timeout=profile.timeout_s,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
                    data = response.json()
                    text = _extract_text(data)
                    if text is None:
                        # A 200 with an unexpected body is a failure, not an
                        # empty result — surface it to the breaker.
                        raise ValueError(f"unparseable response for job={profile.job.value}")
                    # OpenRouter reports the model that actually answered. When
                    # that is not the primary, a fallback absorbed an outage
                    # silently — worth a log line, since the cost profile of the
                    # job just changed.
                    served = data.get("model")
                    if served and served != self._model_for(profile):
                        logger.warning(
                            f"OpenRouter job={profile.job.value} served by fallback "
                            f"{served} (primary {self._model_for(profile)})"
                        )
                    self._record_success()
                    return text
                except Exception as e:  # noqa: BLE001 — breaker needs every failure
                    if attempt < RESILIENCE.max_retries - 1:
                        delay = RESILIENCE.base_delay_s * (2 ** attempt)
                        logger.warning(
                            f"OpenRouter attempt {attempt + 1}/{RESILIENCE.max_retries} "
                            f"for job={profile.job.value} failed: {e}. Retrying in {delay}s..."
                        )
                        await asyncio.sleep(delay)
                    else:
                        self._record_failure(e)
                        return None
        return None

    # -- jobs -------------------------------------------------------------

    async def heal_selector(self, html_chunk: str, target_description: str) -> str | None:
        """Find a replacement CSS selector for a broken field."""
        profile = profile_for(AIJob.HEAL)
        snippet = compact_html_for_prompt(
            sanitize_for_llm(html_chunk), max_chars=profile.max_prompt_chars
        )
        prompt = (
            "Analyze this HTML snippet from a procurement portal.\n"
            f"Identify the CSS selector that leads to: {target_description}.\n"
            "Return ONLY the CSS selector string, no explanation.\n\n"
            f"Snippet:\n{snippet}"
        )
        selector = await self._call(profile, prompt)
        if selector:
            logger.info(f"OpenRouter: healed selector: {selector}")
        return selector

    async def generate_overlay(self, html_sample: str) -> dict[str, Any] | None:
        """Analyze a landing page and generate a declarative extraction overlay."""
        profile = profile_for(AIJob.OVERLAY)
        # Cache on exactly the text sent to the model, so two pages that differ
        # only past the truncation point cannot collide.
        sample = compact_html_for_prompt(
            sanitize_for_llm(html_sample), max_chars=profile.max_prompt_chars
        )
        cache_model = self._model_for(profile)
        cached = await self.cache.get(cache_model, AIJob.OVERLAY.value, sample)
        if isinstance(cached, dict):
            logger.debug("OpenRouter: overlay cache hit")
            return cached

        prompt = (
            "Analyze this HTML from a procurement site.\n"
            "Create a JSON 'overlay' for Spacescraper extraction.\n"
            "Format must be:\n"
            "{\n"
            '    "entity_type": "Opportunity",\n'
            '    "container_selector": "CSS_SELECTOR_FOR_ITEM_WRAPPER",\n'
            '    "field_mappings": {\n'
            '        "title": "SELECTOR",\n'
            '        "buyer": "SELECTOR",\n'
            '        "deadline": "SELECTOR",\n'
            '        "estimated_budget": "SELECTOR",\n'
            '        "url": "SELECTOR_WITH_[href]"\n'
            "    }\n"
            "}\n"
            "Return ONLY the JSON.\n\n"
            f"HTML:\n{sample}"
        )

        overlay = _parse_json(await self._call(profile, prompt))
        if overlay is not None:
            # Write-through. Without this the cache never fills and every
            # request re-bills the same HTML. Failures are never cached.
            await self.cache.set(cache_model, AIJob.OVERLAY.value, sample, overlay)
        return overlay

    async def enrich_opportunity(self, opportunity_data: dict[str, Any]) -> dict[str, Any] | None:
        """Translate to English, summarise, and normalise the budget."""
        profile = profile_for(AIJob.ENRICH)
        safe = redact_pii(opportunity_data) if isinstance(opportunity_data, dict) else opportunity_data
        safe = _sanitize_text_values(safe)
        prompt = (
            "Analyze the following procurement opportunity data.\n"
            "Task:\n"
            "1. Translate the 'title' and 'buyer' into English if they are not.\n"
            "2. Create a concise 2-sentence 'summary' of the project.\n"
            "3. Convert the 'estimated_budget' to EUR (a normalized float). Use current "
            "approx rates. Keep it null if missing or vague.\n"
            "Format the output as ONLY raw JSON:\n"
            '{ "title_en": "...", "buyer_en": "...", "summary": "...", '
            '"normalized_budget_eur": 1500000.0 }\n\n'
            f"Opportunity Data:\n{safe}"
        )[: profile.max_prompt_chars]
        return _parse_json(await self._call(profile, prompt))

    async def compute_embedding(self, text: str) -> list[float] | None:
        """Always None: OpenRouter serves no embedding models.

        All 424 catalogue entries are chat-completion models and there is no
        embeddings route, so with every job routed through OpenRouter there is
        nothing to call. Deduplication falls back to fuzzy title matching rather
        than failing the pipeline. Restoring embeddings means adding a second
        provider account.
        """
        return None

    # -- EnrichmentProvider port -----------------------------------------

    async def generate(self, prompt: str, *, timeout: float | None = None) -> str | None:
        """Free-form generation. `timeout` overrides the job profile when given."""
        profile = profile_for(AIJob.GENERATE)
        if timeout is not None and timeout != profile.timeout_s:
            profile = JobProfile(
                job=profile.job,
                model=profile.model,
                timeout_s=timeout,
                max_prompt_chars=profile.max_prompt_chars,
                temperature=profile.temperature,
                expects_json=profile.expects_json,
            )
        return await self._call(profile, prompt[: profile.max_prompt_chars])

    async def embed(self, text: str) -> list[float] | None:
        return await self.compute_embedding(text)

    async def enrich(self, data: dict[str, Any], prompt_hint: str = "") -> dict[str, Any] | None:
        return await self.enrich_opportunity(data)

    async def is_available(self) -> bool:
        return self._check_circuit()


def _reasoning_block(profile: JobProfile) -> dict[str, Any] | None:
    """Build the `reasoning` request block, or None to say nothing about it.

    Reasoning tokens bill as output tokens, so leaving a model's default in
    place is a cost decision made by the vendor rather than by us. The mapping:

    * job wants reasoning  -> request it at the job's effort, with `exclude`
      set because nothing in this codebase reads the trace and returning it
      only costs bandwidth;
    * job does not want it, model allows opting out -> `enabled: false`;
    * job does not want it, model makes it mandatory -> ask for the cheapest
      supported effort and drop the trace. This is the floor, not off.
    """
    model = profile.model

    if profile.reasoning:
        block: dict[str, Any] = {"exclude": True}
        if profile.reasoning_effort and profile.reasoning_effort in model.supported_efforts:
            block["effort"] = profile.reasoning_effort
        else:
            block["enabled"] = True
        return block

    if not model.reasoning_default_enabled and not model.reasoning_mandatory:
        return None  # already off; saying so would just be noise

    if not model.reasoning_mandatory:
        return {"enabled": False}

    # Mandatory: cannot be switched off, so minimise it instead.
    block = {"exclude": True}
    for effort in ("none", "minimal", "low"):
        if effort in model.supported_efforts:
            block["effort"] = effort
            break
    return block


def _extract_text(data: dict[str, Any]) -> str | None:
    """Pull the assistant message out of an OpenRouter chat response."""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return content.strip() if isinstance(content, str) else None


def _parse_json(raw: str | None) -> dict[str, Any] | None:
    """Parse a model's JSON reply, tolerating a ```json fence."""
    if not raw:
        return None
    try:
        parsed = json.loads(_JSON_FENCE.sub("", raw).strip())
    except json.JSONDecodeError as e:
        logger.error(f"OpenRouter: failed to parse JSON reply: {e}")
        return None
    return parsed if isinstance(parsed, dict) else None
