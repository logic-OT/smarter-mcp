"""
LLM client for description generation.

Uses the OpenAI Python SDK for all providers. OpenAI and OpenRouter are native;
Anthropic is reached via Anthropic's OpenAI-compatible endpoint.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

from smarter_mcp.config.manifest import LLMConfig

logger = logging.getLogger(__name__)


class LLMNotAvailableError(RuntimeError):
    """Raised when no usable LLM backend can be constructed."""


# Per-provider base URL and key env var defaults.
_PROVIDER_DEFAULTS: dict[str, dict[str, str | None]] = {
    "openai": {"base_url": None, "api_key_env": "OPENAI_API_KEY"},
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1/",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
}


def _normalize_provider(provider: str) -> str:
    """Canonicalize provider aliases ('claude' -> 'anthropic')."""
    p = (provider or "").strip().lower()
    return "anthropic" if p == "claude" else p


class LLMClient(ABC):
    """Minimal chat-completion interface used by the description generator."""

    @abstractmethod
    def generate(self, system: str, user: str) -> str:
        """Return a single completion string for the given system/user prompt."""
        raise NotImplementedError


class OpenAIClient(LLMClient):
    """Chat completions via the OpenAI Python SDK (OpenAI, OpenRouter, Anthropic)."""

    def __init__(self, config: LLMConfig):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise LLMNotAvailableError(
                "The 'openai' package is required for LLM description generation. "
                "Install it with: pip install smarter-mcp"
            ) from e

        provider = _normalize_provider(config.provider)
        defaults = _PROVIDER_DEFAULTS.get(provider, _PROVIDER_DEFAULTS["openai"])

        base_url = config.base_url or defaults["base_url"]
        api_key_env = config.api_key_env or defaults["api_key_env"]
        api_key = os.environ.get(api_key_env or "", "")
        if not api_key:
            raise LLMNotAvailableError(
                f"No API key found in environment variable '{api_key_env}'. "
                f"Set it to enable LLM description generation."
            )

        self._model = config.model
        self._max_tokens = config.max_tokens
        self._temperature = config.temperature
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def generate(self, system: str, user: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )
        return (resp.choices[0].message.content or "").strip()


def build_llm_client(config: LLMConfig) -> LLMClient:
    """Construct an OpenAI SDK client for the given config."""
    return OpenAIClient(config)
