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
import re
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

# Maximum length (chars) for a cached description.
_MAX_DESC_LEN = 500

# Regex to strip paired markdown code fences (``` ... ```) from LLM output.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
# Fallback: strip any remaining unterminated opening fence (e.g. ```python with no closing ```).
_UNTERMINATED_FENCE_RE = re.compile(r"```.*$", re.DOTALL)

# Error class names that indicate ALL future LLM calls will also fail.
# Using class name strings avoids a hard dependency on the openai package.
_ABORT_ON_ERROR_TYPES: frozenset[str] = frozenset({
    "AuthenticationError",
    "APIConnectionError",
})


def _sanitize_description(text: str) -> str:
    """Strip markdown code fences and cap length for safe caching.

    LLM output occasionally includes fenced code blocks despite system prompt
    instructions. We strip them and cap the length to prevent unbounded cache
    growth and to ensure descriptions fit in tool schemas.
    """
    # Strip paired fences (e.g. ```python\ncode\n```).
    text = _FENCE_RE.sub("", text).strip()
    # Strip any remaining unterminated opening fence (e.g. ```python with no closing ```).
    text = _UNTERMINATED_FENCE_RE.sub("", text).strip()
    if len(text) > _MAX_DESC_LEN:
        # Truncate at a word boundary to avoid cutting mid-word.
        truncated = text[:_MAX_DESC_LEN]
        last_space = truncated.rfind(" ")
        text = (truncated[:last_space] if last_space > 0 else truncated) + "…"
    return text


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

        Raises:
            LLMNotAvailableError: when the LLM backend cannot be constructed
                (e.g. openai not installed, no API key).
            Exception with type name in _ABORT_ON_ERROR_TYPES: re-raised so
                ``enrich_registry`` can abort the whole enrichment run. All
                other per-tool failures are logged and suppressed here (None
                is returned), so a single bad tool does not abort the run.
        """
        signature = self._build_signature(tool)
        docstring = tool.description or ""
        key = self._cache_key(signature, docstring)

        if key in self._cache:
            return self._cache[key]

        try:
            client = self._get_client()
            user_prompt = self._build_user_prompt(signature, docstring)
            raw = client.generate(_SYSTEM_PROMPT, user_prompt)
            description = _sanitize_description(raw)
        except LLMNotAvailableError:
            raise
        except Exception as e:
            # Auth/connection errors signal that all future calls will also fail;
            # re-raise them so enrich_registry can abort the loop.
            if type(e).__name__ in _ABORT_ON_ERROR_TYPES:
                raise
            # Per-tool content/rate errors: log and skip this tool only.
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
        returning, pruning stale entries (tools no longer in the registry).
        """
        written = 0
        # Collect the cache key for every tool currently in the registry so we
        # can prune stale entries (from deleted/renamed tools) before saving.
        active_keys: set[str] = set()

        for tool in registry.get_all_tools():
            sig = self._build_signature(tool)
            doc = tool.description or ""
            active_keys.add(self._cache_key(sig, doc))

            if not self._needs_description(tool):
                continue

            try:
                description = self.generate_for_tool(tool)
            except LLMNotAvailableError:
                raise
            except Exception as e:
                # Only abort-class errors (AuthenticationError, APIConnectionError)
                # reach here — generate_for_tool swallows per-tool content/rate
                # failures internally and returns None for them.
                logger.error(
                    "LLM enrichment aborted: %s — %s. "
                    "Check API key and network connectivity.",
                    type(e).__name__, e,
                )
                break

            if description:
                tool.description = description
                written += 1

        # Prune cache entries whose tools are no longer in this registry.
        # This prevents unbounded growth from renamed or deleted tools.
        stale = set(self._cache) - active_keys
        if stale:
            for k in stale:
                del self._cache[k]
            self._dirty = True

        self.save_cache()
        if written:
            logger.info("LLM generated %d tool description(s).", written)
        return written
