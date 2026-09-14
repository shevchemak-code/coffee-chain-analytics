"""LLM provider abstraction.

One small interface (LLMProvider.complete) with two real implementations
(Anthropic, OpenAI) selected via environment variables, plus an explicit
NoProvider sentinel used when no API key is configured — so the app can run
and its deterministic parts remain testable without any external calls.

Configuration (environment variables):
    LLM_PROVIDER   "anthropic" | "openai"   (default: anthropic)
    ANTHROPIC_API_KEY / OPENAI_API_KEY
    LLM_MODEL      override the default model for the chosen provider
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_TOKENS = 1500


class ProviderError(Exception):
    """The provider call failed (network, auth, rate limit, malformed reply)."""


class ConfigError(Exception):
    """The provider is not configured (missing key, unknown provider)."""


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: dict = field(default_factory=dict)


class LLMProvider:
    """Interface. `system` is the full system prompt text, `user` the user message."""

    name = "base"

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        json_schema: Optional[dict] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> LLMResponse:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class AnthropicProvider(LLMProvider):
    """Anthropic Messages API. json_mode is implemented via forced tool use so the
    output is guaranteed-parseable JSON conforming to json_schema."""

    name = "anthropic"
    DEFAULT_MODEL = "claude-haiku-4-5-20251001"
    BASE_URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("LLM_MODEL") or self.DEFAULT_MODEL
        if not self.api_key:
            raise ConfigError("ANTHROPIC_API_KEY is not set")

    def complete(self, system, user, *, json_mode=False, json_schema=None,
                 max_tokens=DEFAULT_MAX_TOKENS, timeout=DEFAULT_TIMEOUT) -> LLMResponse:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if json_mode and json_schema:
            payload["tools"] = [
                {
                    "name": "emit_digest",
                    "description": "Emit the structured digest as JSON.",
                    "input_schema": json_schema,
                }
            ]
            payload["tool_choice"] = {"type": "tool", "name": "emit_digest"}
        try:
            resp = httpx.post(self.BASE_URL, headers=headers, json=payload,
                              timeout=timeout)
        except httpx.HTTPError as exc:
            raise ProviderError(f"anthropic request failed: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(
                f"anthropic HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        stop = data.get("stop_reason", "")
        if stop not in ("end_turn", "tool_use"):
            raise ProviderError(f"anthropic stop_reason={stop!r} (truncated or blocked)")
        text_parts, tool_inputs = [], []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_inputs.append(block.get("input", {}))
        if tool_inputs:
            import json
            text = json.dumps(tool_inputs[0])
        else:
            text = "\n".join(text_parts).strip()
        if not text:
            raise ProviderError("anthropic returned an empty response")
        return LLMResponse(text=text, model=self.model,
                           usage=data.get("usage") or {})


class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions. json_mode maps to response_format json_object."""

    name = "openai"
    DEFAULT_MODEL = "gpt-4o-mini"
    BASE_URL = "https://api.openai.com/v1/chat/completions"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model or os.environ.get("LLM_MODEL") or self.DEFAULT_MODEL
        if not self.api_key:
            raise ConfigError("OPENAI_API_KEY is not set")

    def complete(self, system, user, *, json_mode=False, json_schema=None,
                 max_tokens=DEFAULT_MAX_TOKENS, timeout=DEFAULT_TIMEOUT) -> LLMResponse:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            resp = httpx.post(self.BASE_URL, headers=headers, json=payload,
                              timeout=timeout)
        except httpx.HTTPError as exc:
            raise ProviderError(f"openai request failed: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"openai HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"openai malformed response: {exc}") from exc
        if not text:
            raise ProviderError("openai returned an empty response")
        return LLMResponse(text=text, model=self.model,
                           usage=data.get("usage") or {})


class NoProvider(LLMProvider):
    """Explicit sentinel: AI features degrade instead of crashing."""

    name = "none"

    def __init__(self, reason: str = "no LLM provider configured"):
        self.reason = reason

    def complete(self, *args, **kwargs) -> LLMResponse:
        raise ConfigError(self.reason)

    def describe(self) -> str:
        return f"none ({self.reason})"


def get_provider() -> LLMProvider:
    """Build the configured provider, or NoProvider if unconfigured/unavailable."""
    which = os.environ.get("LLM_PROVIDER", "anthropic").lower().strip()
    try:
        if which == "anthropic":
            return AnthropicProvider()
        if which == "openai":
            return OpenAIProvider()
        return NoProvider(f"unknown LLM_PROVIDER {which!r}")
    except ConfigError as exc:
        return NoProvider(str(exc))
