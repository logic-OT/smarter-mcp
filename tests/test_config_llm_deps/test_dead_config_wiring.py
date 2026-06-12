"""Tests for wiring previously-dead config fields:
- server.log_level → applied at server startup
- multimodal.auto_detect → gates image coercion in tool_wrapper
- SourceConfig.include for path sources → filters files in SurfaceExtractor
"""
from __future__ import annotations

import logging

import pytest

from smarter_mcp._decorators import clear_global_registry


@pytest.fixture(autouse=True)
def _reset():
    import sys
    before = set(sys.modules.keys())
    clear_global_registry()
    yield
    # Remove any modules that were imported during the test so they don't
    # collide with same-named modules in other test directories.
    added = set(sys.modules.keys()) - before
    for key in added:
        del sys.modules[key]
    clear_global_registry()


# ---------------------------------------------------------------------------
# log_level wiring
# ---------------------------------------------------------------------------

class TestLogLevelWiring:
    def test_log_level_debug_applied_at_init(self, tmp_path):
        """When manifest.server.log_level='debug', root logger level becomes DEBUG."""
        from smarter_mcp.server.app import SmarterMCP

        mf = tmp_path / "smarter-mcp.yaml"
        mf.write_text("name: debug-test\nserver:\n  log_level: debug\n")
        _app = SmarterMCP(manifest=str(mf))
        root_level = logging.getLogger().level
        assert root_level == logging.DEBUG, (
            f"Expected root logger level=DEBUG (10), got {root_level}"
        )

    def test_log_level_warning_applied(self, tmp_path):
        """server.log_level='warning' must set root logger to WARNING."""
        from smarter_mcp.server.app import SmarterMCP

        mf = tmp_path / "smarter-mcp.yaml"
        mf.write_text("name: warn-test\nserver:\n  log_level: warning\n")
        _app = SmarterMCP(manifest=str(mf))
        root_level = logging.getLogger().level
        assert root_level == logging.WARNING, (
            f"Expected WARNING (30), got {root_level}"
        )

    def test_log_level_invalid_does_not_crash(self, tmp_path):
        """An unrecognised log_level must silently not apply rather than crashing startup."""
        from smarter_mcp.server.app import SmarterMCP

        mf = tmp_path / "smarter-mcp.yaml"
        mf.write_text("name: safe-test\nserver:\n  log_level: verbose\n")
        # Must not raise
        _app = SmarterMCP(manifest=str(mf))


# ---------------------------------------------------------------------------
# auto_detect wiring
# ---------------------------------------------------------------------------

class TestAutoDetectWiring:
    def test_auto_detect_false_skips_image_coercion(self):
        """With auto_detect=False, a plain-string-returning tool must not be
        fed through coerce_to_fastmcp_image."""
        from smarter_mcp._registry import RegisteredTool
        from smarter_mcp.runtime.tool_wrapper import build_tool_wrapper

        def my_tool(x: str) -> str:
            return f"hello {x}"

        tool = RegisteredTool(
            fn=my_tool,
            name="my_tool",
            namespace="default",
            description=None,
            source="decorator",
        )

        wrapper = build_tool_wrapper(tool, my_tool, auto_detect=False)
        result = wrapper(x="world")
        assert result == "hello world", (
            f"Expected 'hello world', got {result!r}. "
            "auto_detect=False must skip image coercion."
        )

    def test_auto_detect_true_is_default(self):
        """build_tool_wrapper must default to auto_detect=True (no TypeError)."""
        import inspect

        from smarter_mcp._registry import RegisteredTool
        from smarter_mcp.runtime.tool_wrapper import build_tool_wrapper

        def my_tool(x: str) -> str:
            return "hi"

        tool = RegisteredTool(
            fn=my_tool,
            name="my_tool",
            namespace="default",
            description=None,
            source="decorator",
        )
        wrapper = build_tool_wrapper(tool, my_tool)
        sig = inspect.signature(wrapper)
        assert "x" in sig.parameters

    @pytest.mark.asyncio
    async def test_router_passes_auto_detect_false_to_wrapper(self, tmp_path):
        """NamespaceRouter must read multimodal.auto_detect from manifest and
        pass it to build_tool_wrapper so string returns are not coerced.

        Calls the tool through the real in-memory FastMCP Client to prove
        auto_detect=False is wired end-to-end (not just unit-tested at the
        wrapper level).  With auto_detect=True, "Hello, Alice!" would be
        treated as an image path and raise an error.
        """
        from fastmcp import Client

        from smarter_mcp import tool
        from smarter_mcp.server.app import SmarterMCP

        @tool("greet tool")
        def greet(name: str) -> str:
            return f"Hello, {name}!"

        mf = tmp_path / "smarter-mcp.yaml"
        mf.write_text(
            "name: auto-detect-test\n"
            "multimodal:\n"
            "  auto_detect: false\n"
        )
        app = SmarterMCP(manifest=str(mf))
        server = app.build()

        async with Client(server) as client:
            res = await client.call_tool("greet", {"name": "Alice"})
            # Extract the text value from the CallToolResult
            if hasattr(res, "content") and res.content:
                value = getattr(res.content[0], "text", None) or str(res.content[0])
            elif hasattr(res, "data") and res.data is not None:
                value = str(res.data)
            else:
                value = str(res)
            assert value == "Hello, Alice!", (
                f"Expected 'Hello, Alice!', got {value!r}. "
                "auto_detect=False must not coerce plain string returns to images."
            )


# ---------------------------------------------------------------------------
# SourceConfig.include for path sources
# ---------------------------------------------------------------------------

class TestSourceConfigIncludePathSources:
    def test_include_pattern_filters_files(self, tmp_path):
        """SourceConfig.include must restrict which files are scanned for path sources."""
        (tmp_path / "tools.py").write_text(
            "def greet(name: str) -> str:\n    return f'hello {name}'\n"
        )
        (tmp_path / "internal.py").write_text(
            "def secret(x: int) -> int:\n    return x * 2\n"
        )

        mf = tmp_path / "smarter-mcp.yaml"
        mf.write_text(
            "name: test\n"
            "sources:\n"
            "  - path: .\n"
            "    include:\n"
            "      - tools.py\n"
        )

        from smarter_mcp.server.app import SmarterMCP
        app = SmarterMCP(manifest=str(mf), use_inspect=False)
        app.build()

        tool_names = {t.name for t in app._registry.get_all_tools()}
        assert "greet" in tool_names, (
            f"'greet' (from tools.py) should be discovered; got {tool_names}"
        )
        assert "secret" not in tool_names, (
            f"'secret' (from internal.py) should be excluded by include=[tools.py]; "
            f"got {tool_names}"
        )

    def test_empty_include_scans_all_files(self, tmp_path):
        """SourceConfig.include=[] (empty/absent) must scan all files."""
        (tmp_path / "a.py").write_text(
            "def func_a(x: int) -> int:\n    return x\n"
        )
        (tmp_path / "b.py").write_text(
            "def func_b(x: int) -> int:\n    return x\n"
        )

        mf = tmp_path / "smarter-mcp.yaml"
        mf.write_text(
            "name: test\n"
            "sources:\n"
            "  - path: .\n"
        )

        from smarter_mcp.server.app import SmarterMCP
        app = SmarterMCP(manifest=str(mf), use_inspect=False)
        app.build()

        tool_names = {t.name for t in app._registry.get_all_tools()}
        assert "func_a" in tool_names
        assert "func_b" in tool_names

    def test_surface_extractor_include_patterns(self, tmp_path):
        """SurfaceExtractor with include_patterns must only yield matching files."""
        from smarter_mcp.extractor.surface import SurfaceExtractor

        (tmp_path / "keep.py").write_text(
            "def kept_fn(x: int) -> int:\n    return x\n"
        )
        (tmp_path / "skip.py").write_text(
            "def skipped_fn(x: int) -> int:\n    return x\n"
        )

        extractor = SurfaceExtractor(
            source_root=tmp_path,
            use_inspect=False,
            include_patterns=["keep.py"],
        )
        result = extractor.extract()

        all_fn_names = {
            fn.simple_name
            for mod in result.modules
            for fn in mod.functions
        }
        assert "kept_fn" in all_fn_names, (
            f"kept_fn should be extracted; got {all_fn_names}"
        )
        assert "skipped_fn" not in all_fn_names, (
            f"skipped_fn should be excluded by include_patterns; got {all_fn_names}"
        )
