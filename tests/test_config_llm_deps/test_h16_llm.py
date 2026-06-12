"""Tests for H16 — LLM client reliability + router description fix + cache pruning."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from smarter_mcp._registry import RegisteredTool, ToolRegistry
from smarter_mcp.config.manifest import LLMConfig
from smarter_mcp.llm.client import LLMNotAvailableError, OpenAIClient
from smarter_mcp.llm.generator import LLMGenerator
from smarter_mcp.server.router import _build_tool_description


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry_with_tools(names: list[str]) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        def fn(**kw):
            return None
        fn.__name__ = name
        registry.register_tool(fn, name=name, namespace="default")
    return registry


def _make_config(tmp_path, **kwargs) -> LLMConfig:
    defaults = dict(
        enabled=True,
        provider="openai",
        api_key_env="TEST_KEY",
        cache_path=str(tmp_path / "desc_cache.json"),
    )
    defaults.update(kwargs)
    return LLMConfig(**defaults)


# ---------------------------------------------------------------------------
# H16-a: timeout and max_retries passed to OpenAI SDK constructor
# ---------------------------------------------------------------------------

class TestOpenAIClientTimeout:
    def test_timeout_passed_to_client_constructor(self, monkeypatch, tmp_path):
        """OpenAIClient must pass timeout= to the OpenAI SDK constructor."""
        captured_kwargs = {}

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured_kwargs.update(kwargs)

        monkeypatch.setenv("TEST_KEY", "sk-test")

        # The import `from openai import OpenAI` is inside __init__, so we must
        # patch at the openai module level (not at smarter_mcp.llm.client.OpenAI).
        with patch("openai.OpenAI", FakeOpenAI):
            config = _make_config(tmp_path)
            try:
                OpenAIClient(config)
            except Exception:
                pass  # FakeOpenAI has no chat attr

        assert "timeout" in captured_kwargs, (
            "OpenAI SDK must be constructed with explicit timeout= to avoid 600s default"
        )
        assert isinstance(captured_kwargs["timeout"], (int, float)), (
            f"timeout must be numeric, got {captured_kwargs['timeout']!r}"
        )
        assert 5 <= captured_kwargs["timeout"] <= 60, (
            f"timeout {captured_kwargs['timeout']} is outside reasonable 5-60s range"
        )

    def test_max_retries_passed_to_client_constructor(self, monkeypatch, tmp_path):
        """OpenAIClient must pass max_retries= to the OpenAI SDK constructor."""
        captured_kwargs = {}

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured_kwargs.update(kwargs)

        monkeypatch.setenv("TEST_KEY", "sk-test")

        with patch("openai.OpenAI", FakeOpenAI):
            config = _make_config(tmp_path)
            try:
                OpenAIClient(config)
            except Exception:
                pass

        assert "max_retries" in captured_kwargs, (
            "OpenAI SDK must be constructed with explicit max_retries="
        )
        assert captured_kwargs["max_retries"] in (1, 2), (
            f"max_retries should be 1 or 2, got {captured_kwargs['max_retries']!r}"
        )


# ---------------------------------------------------------------------------
# H16-b: abort enrichment on auth/connection errors
# ---------------------------------------------------------------------------

class TestEnrichAbortOnAuthError:
    def test_abort_on_authentication_error(self, tmp_path):
        """enrich_registry must abort on AuthenticationError (not retry per-tool)."""
        config = _make_config(tmp_path)
        call_count = 0

        class AuthError(Exception):
            pass
        AuthError.__name__ = "AuthenticationError"

        class FakeClient:
            def generate(self, system, user):
                nonlocal call_count
                call_count += 1
                raise AuthError("401 Unauthorized")

        registry = _make_registry_with_tools(["tool_a", "tool_b", "tool_c"])
        gen = LLMGenerator(config, client=FakeClient())
        gen.enrich_registry(registry)

        assert call_count <= 1, (
            f"Expected at most 1 call after AuthenticationError, got {call_count}. "
            "Enrichment must abort on auth errors."
        )

    def test_abort_on_connection_error(self, tmp_path):
        """enrich_registry must abort on APIConnectionError."""
        config = _make_config(tmp_path)
        call_count = 0

        class ConnError(Exception):
            pass
        ConnError.__name__ = "APIConnectionError"

        class FakeClient:
            def generate(self, system, user):
                nonlocal call_count
                call_count += 1
                raise ConnError("Connection refused")

        registry = _make_registry_with_tools(["t1", "t2", "t3"])
        gen = LLMGenerator(config, client=FakeClient())
        gen.enrich_registry(registry)

        assert call_count <= 1, (
            f"Expected abort after APIConnectionError, got {call_count} calls"
        )

    def test_per_tool_content_error_continues(self, tmp_path):
        """Non-auth errors per tool must NOT abort the whole enrichment run."""
        config = _make_config(tmp_path)
        call_count = 0

        class ContentError(Exception):
            pass
        ContentError.__name__ = "BadRequestError"

        class FakeClient:
            def generate(self, system, user):
                nonlocal call_count
                call_count += 1
                raise ContentError("content policy violation")

        registry = _make_registry_with_tools(["t1", "t2", "t3"])
        gen = LLMGenerator(config, client=FakeClient())
        gen.enrich_registry(registry)

        assert call_count == 3, (
            f"Expected 3 calls (one per tool) for non-auth errors, got {call_count}"
        )


# ---------------------------------------------------------------------------
# H16-c: description sanitization
# ---------------------------------------------------------------------------

class TestDescriptionSanitization:
    def test_markdown_fence_stripped(self, tmp_path):
        """Descriptions with code fences must be stripped before caching."""
        config = _make_config(tmp_path)

        class FakeClient:
            def generate(self, system, user):
                return "```python\nsome_code()\n```\nActual description here."

        registry = _make_registry_with_tools(["my_tool"])
        gen = LLMGenerator(config, client=FakeClient())
        gen.enrich_registry(registry)

        tools = list(registry.get_all_tools())
        desc = tools[0].description or ""
        assert "```" not in desc, (
            f"Markdown fences should be stripped; got: {desc!r}"
        )
        assert "Actual description" in desc, (
            f"Real content after fence should survive; got: {desc!r}"
        )

    def test_description_length_capped(self, tmp_path):
        """Generated descriptions must be capped at a reasonable length."""
        config = _make_config(tmp_path)

        long_desc = "word " * 200  # ~1000 chars

        class FakeClient:
            def generate(self, system, user):
                return long_desc

        registry = _make_registry_with_tools(["my_tool"])
        gen = LLMGenerator(config, client=FakeClient())
        gen.enrich_registry(registry)

        tools = list(registry.get_all_tools())
        desc = tools[0].description or ""
        assert len(desc) <= 600, (
            f"Description length {len(desc)} exceeds 600-char cap"
        )

    def test_clean_description_passes_through(self, tmp_path):
        """A clean one-sentence description must not be mangled."""
        config = _make_config(tmp_path)
        expected = "Adds two numbers and returns their sum."

        class FakeClient:
            def generate(self, system, user):
                return expected

        registry = _make_registry_with_tools(["add"])
        gen = LLMGenerator(config, client=FakeClient())
        gen.enrich_registry(registry)

        tools = list(registry.get_all_tools())
        assert tools[0].description == expected


# ---------------------------------------------------------------------------
# H16-d: router must NOT truncate multi-line descriptions
# ---------------------------------------------------------------------------

class TestRouterDescriptionNotTruncated:
    def _make_tool(self, description: str | None) -> RegisteredTool:
        def fn():
            return None
        fn.__name__ = "my_tool"
        return RegisteredTool(
            fn=fn,
            name="my_tool",
            namespace="default",
            description=description,
            source="decorator",
        )

    def test_multiline_description_not_truncated(self):
        """_build_tool_description must return the full description, not just the first line."""
        desc = "First line summary.\n\nMore detailed explanation here.\nAnd a third line."
        tool = self._make_tool(desc)
        result = _build_tool_description(tool)
        assert result == desc.strip(), (
            f"Expected full description, got: {result!r}"
        )

    def test_single_line_description_preserved(self):
        desc = "A single-line description."
        tool = self._make_tool(desc)
        result = _build_tool_description(tool)
        assert result == desc

    def test_auto_generated_when_no_description(self):
        """When no description is set, auto-generate a placeholder containing the tool name."""
        tool = self._make_tool(None)
        result = _build_tool_description(tool)
        assert "my_tool" in result


# ---------------------------------------------------------------------------
# Cache pruning
# ---------------------------------------------------------------------------

class TestCachePruning:
    def test_stale_cache_entries_pruned_on_save(self, tmp_path):
        """enrich_registry must prune entries whose tools are no longer in the registry."""
        cache_path = tmp_path / "cache.json"
        config = _make_config(tmp_path, cache_path=str(cache_path))

        # Seed the cache with a stale entry (sha256-like key, 64 hex chars)
        stale_key = "deadbeef" * 8
        initial_cache = {stale_key: "A stale description for a deleted tool."}
        cache_path.write_text(json.dumps(initial_cache))

        call_count = 0

        class FakeClient:
            def generate(self, system, user):
                nonlocal call_count
                call_count += 1
                return "Fresh description."

        registry = _make_registry_with_tools(["current_tool"])
        gen = LLMGenerator(config, client=FakeClient())
        gen.enrich_registry(registry)

        saved = json.loads(cache_path.read_text())
        assert stale_key not in saved, (
            f"Stale cache key should have been pruned; cache: {saved}"
        )

    def test_current_entries_not_pruned(self, tmp_path):
        """Cache entries for current tools must not be deleted on a fresh run.

        Simulates a server restart: fresh registry (tools start with no
        descriptions), second LLMGenerator loads cache from disk.  The
        key stored in the cache uses doc="" (pre-enrichment), and on a
        fresh registry the second run also computes doc="" — so the entry
        must survive the prune step.
        """
        cache_path = tmp_path / "cache.json"
        config = _make_config(tmp_path, cache_path=str(cache_path))

        # First pass: generate and cache descriptions
        class FakeClient:
            def generate(self, system, user):
                return "Good description."

        registry1 = _make_registry_with_tools(["my_tool"])
        gen = LLMGenerator(config, client=FakeClient())
        gen.enrich_registry(registry1)

        saved_first = json.loads(cache_path.read_text())
        assert len(saved_first) == 1, f"Expected 1 entry, got {saved_first}"

        # Second pass: fresh registry (simulating server restart).
        # Tools start with no descriptions → active_keys uses doc="",
        # matching the cached entry → entry must NOT be pruned.
        registry2 = _make_registry_with_tools(["my_tool"])
        gen2 = LLMGenerator(config)  # fresh generator loads cache from disk
        gen2.enrich_registry(registry2)

        saved_second = json.loads(cache_path.read_text())
        assert len(saved_second) >= 1, (
            "Cache entry for current tool should not be pruned on fresh registry"
        )
