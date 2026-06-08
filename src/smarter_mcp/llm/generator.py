"""
LLM-assisted tool description generation.

`LLMGenerator` walks the `ToolRegistry` and fills in missing (or, optionally,
all) tool descriptions by prompting an LLM with each tool's signature and any
existing docstring. Results are cached on disk keyed by the prompt content, so
re-running over an unchanged codebase makes zero LLM calls.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from pathlib import Path

from smarter_mcp._registry import RegisteredTool, ToolRegistry
from smarter_mcp.config.manifest import LLMConfig
from smarter_mcp.llm.client import LLMNotAvailableError, build_llm_client

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You write concise, accurate descriptions for Python functions that are "
    "exposed as tools to an AI agent. Given a function signature and any "
    "existing docstring, respond with a SINGLE plain-text sentence describing "
    "what the tool does and when to use it. Do not use markdown, code fences, "
    "or quotes. Do not restate the parameter list."
)


class LLMGenerator:
    """Generates and caches tool descriptions via an LLM."""

    def __init__(self, config: LLMConfig, *, client=None):
        """
        Args:
            config: LLM configuration (provider, model, cache path, etc.).
            client: Optional pre-built client (mainly for testing). If None,
                a client is constructed lazily on first use.
        """
        self.config = config
        self._client = client
        self._cache_path = Path(config.cache_path)
        self._cache: dict[str, str] = self._load_cache()
        self._dirty = False

    # ── cache ──────────────────────────────────────────────────────────

    def _load_cache(self) -> dict[str, str]:
        if self._cache_path.exists():
            try:
                data = json.loads(self._cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Could not read description cache %s: %s", self._cache_path, e)
        return {}

    def save_cache(self) -> None:
        """Persist the description cache to disk (if it changed)."""
        if not self._dirty:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(self._cache, indent=2, sort_keys=True), encoding="utf-8"
            )
            self._dirty = False
        except OSError as e:
            logger.warning("Could not write description cache %s: %s", self._cache_path, e)

    @staticmethod
    def _cache_key(signature: str, docstring: str) -> str:
        digest = hashlib.sha256(f"{signature}\n---\n{docstring}".encode("utf-8")).hexdigest()
        return digest

    # ── client (lazy) ──────────────────────────────────────────────────

    def _get_client(self):
        if self._client is None:
            self._client = build_llm_client(self.config)
        return self._client

    # ── prompt building ────────────────────────────────────────────────

    @staticmethod
    def _build_signature(tool: RegisteredTool) -> str:
        """Render a readable signature string for the prompt."""
        obj = tool.extracted_obj
        if obj is not None:
            params = ", ".join(
                f"{p.name}: {p.effective_type or 'Any'}" for p in obj.non_self_params
            )
            ret = f" -> {obj.return_type}" if obj.return_type else ""
            prefix = f"{obj.class_name}." if obj.class_name else ""
            return f"{prefix}{tool.name}({params}){ret}"
        try:
            return f"{tool.name}{inspect.signature(tool.fn)}"
        except (ValueError, TypeError):
            return f"{tool.name}(...)"

    def _build_user_prompt(self, signature: str, docstring: str) -> str:
        parts = [f"Function signature:\n{signature}"]
        if docstring.strip():
            parts.append(f"\nExisting docstring:\n{docstring.strip()}")
        parts.append("\nWrite the one-sentence tool description.")
        return "\n".join(parts)

    # ── public API ─────────────────────────────────────────────────────

    def _needs_description(self, tool: RegisteredTool) -> bool:
        if self.config.overwrite_existing:
            return True
        desc = (tool.description or "").strip()
        return not desc

    def generate_for_tool(self, tool: RegisteredTool) -> str | None:
        """Generate (or fetch from cache) a description for a single tool.

        Returns the description string, or None if generation failed.
        """
        signature = self._build_signature(tool)
        docstring = tool.description or ""
        key = self._cache_key(signature, docstring)

        if key in self._cache:
            return self._cache[key]

        try:
            client = self._get_client()
            user_prompt = self._build_user_prompt(signature, docstring)
            description = client.generate(_SYSTEM_PROMPT, user_prompt).strip()
        except LLMNotAvailableError:
            raise
        except Exception as e:  # noqa: BLE001 - one bad tool shouldn't abort the run
            logger.warning("LLM description failed for tool '%s': %s", tool.name, e)
            return None

        if not description:
            return None

        self._cache[key] = description
        self._dirty = True
        return description

    def enrich_registry(self, registry: ToolRegistry) -> int:
        """Fill in descriptions for tools across the whole registry.

        Returns the number of descriptions written. Persists the cache before
        returning.
        """
        written = 0
        for tool in registry.get_all_tools():
            if not self._needs_description(tool):
                continue
            description = self.generate_for_tool(tool)
            if description:
                tool.description = description
                written += 1

        self.save_cache()
        if written:
            logger.info("LLM generated %d tool description(s).", written)
        return written
