"""
LLM Client — Single-Call, Bounded, Deterministic JSON Output.
No retries. Rejects malformed output. Enforces token budget.

# === LLM BOUNDARY: ALL LLM I/O CROSSES THIS MODULE ONLY ===
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional


# === LLM BOUNDARY BEGIN ===

@dataclass
class LLMResponse:
    content: str
    tokens_used: int
    prompt_tokens: int
    completion_tokens: int
    raw: Dict[str, Any]


class LLMBudgetExceeded(Exception):
    """Raised when token budget is insufficient."""


class LLMMalformedOutput(Exception):
    """Raised when LLM response cannot be parsed as valid JSON."""


class LLMClient:
    """
    Strict LLM client.
    - Single call per invocation (no retries, no recursion)
    - Hard token budget enforcement (rejects before call if budget <= 0)
    - JSON-only responses; rejects non-JSON
    - Explicit timeout handling

    Constraints (Global Contract §4, §5):
    - Token usage bounded
    - All outputs persisted by caller
    - No implicit fallback
    """

    DEFAULT_MODEL   = "claude-3-haiku-20240307"
    DEFAULT_TIMEOUT = 30  # seconds

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._model   = model
        self._timeout = timeout

    def call(
        self,
        prompt: str,
        inputs: Dict[str, Any],
        max_tokens: int,
        token_budget: int,
    ) -> LLMResponse:
        """
        Make a single LLM call.

        Args:
            prompt: System prompt (canonical version from prompt contract).
            inputs: Variables interpolated into the user message.
            max_tokens: Hard cap on completion tokens.
            token_budget: Remaining budget from planner. Call rejected if <= 0.

        Returns:
            LLMResponse with content and token usage.

        Raises:
            LLMBudgetExceeded: Budget is zero or negative.
            LLMMalformedOutput: Response is not valid JSON.
            RuntimeError: API / network failure.
        """
        # --- Budget gate ---
        if token_budget <= 0:
            raise LLMBudgetExceeded(f"Token budget exhausted: {token_budget}")
        if max_tokens > token_budget:
            max_tokens = token_budget  # clamp to budget

        user_message = self._build_user_message(inputs)

        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": prompt,
            "messages": [{"role": "user", "content": user_message}],
        }

        # === LLM I/O POINT ===
        raw_response = self._http_call(payload)
        # === END LLM I/O ===

        content_text = raw_response["content"][0]["text"]
        usage        = raw_response.get("usage", {})
        tokens_used  = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

        # Strict JSON parse
        try:
            json.loads(content_text)
        except json.JSONDecodeError as exc:
            raise LLMMalformedOutput(
                f"LLM response is not valid JSON: {exc}\nContent: {content_text[:200]}"
            ) from exc

        return LLMResponse(
            content=content_text,
            tokens_used=tokens_used,
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            raw=raw_response,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_user_message(self, inputs: Dict[str, Any]) -> str:
        lines = ["Respond with valid JSON only. No prose, no markdown.\n"]
        for k, v in inputs.items():
            lines.append(f"{k}:\n{json.dumps(v, default=str)}")
        return "\n".join(lines)

    def _http_call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload).encode()
        req  = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=data,
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         self._api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            raise RuntimeError(f"LLM API HTTP {exc.code}: {body[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM API network error: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"LLM API timeout after {self._timeout}s") from exc

# === LLM BOUNDARY END ===
