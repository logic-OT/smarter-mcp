"""Tests for C4 — discover_module for packages, classes, and explicit-include bypass."""

from __future__ import annotations

import pytest

from smarter_mcp import SmarterMCP
from smarter_mcp._decorators import clear_global_registry


@pytest.fixture(autouse=True)
def _reset():
    clear_global_registry()
    yield
    clear_global_registry()


class TestDiscoverModulePackage:
    def test_package_registers_named_tools(self):
        """discover_module on a package with include=[...] must register those tools."""
        import json

        app = SmarterMCP(name="pkg-test")
        app.discover_module(json, include=["dumps", "loads"])
        tools = app._registry.get_all_tools()
        names = {t.name for t in tools}
        assert "dumps" in names, f"Expected 'dumps' in tools; got {names}"
        assert "loads" in names, f"Expected 'loads' in tools; got {names}"

    def test_package_include_bypasses_variadic_skip(self):
        """Explicitly included names must register even when they have *args/**kwargs."""
        import json

        # json.loads has **kw — without the explicit_includes bypass it would be
        # filtered by the variadic policy. With the fix it must survive.
        app = SmarterMCP(name="variadic-test")
        app.discover_module(json, include=["loads"])
        tools = app._registry.get_all_tools()
        names = {t.name for t in tools}
        assert "loads" in names, (
            f"'loads' was not registered; variadic-skip policy is overriding "
            f"explicit include=[]. Got tools: {names}"
        )

    def test_package_respects_exclude(self):
        """discover_module with exclude=[...] must not register those names."""
        import json

        app = SmarterMCP(name="exclude-test")
        app.discover_module(json, include=["dumps", "loads"], exclude=["dumps"])
        tools = app._registry.get_all_tools()
        names = {t.name for t in tools}
        assert "dumps" not in names, f"'dumps' should have been excluded; got {names}"
        assert "loads" in names, f"'loads' should still be present; got {names}"


class TestDiscoverModuleClass:
    def test_class_registers_with_class_name(self):
        """discover_module on a class must register tools with class_name set."""
        from collections import Counter

        app = SmarterMCP(name="class-test")
        app.discover_module(Counter, include=["most_common"])
        tools = app._registry.get_all_tools()
        assert len(tools) >= 1, "Expected at least one tool from Counter.most_common"
        for t in tools:
            assert t.class_name is not None, (
                f"class_name must not be None for a method tool; got {t!r}"
            )
            assert t.class_name == "Counter", (
                f"Expected class_name='Counter', got {t.class_name!r}"
            )

    def test_class_parameters_populated(self):
        """Methods discovered from a class must have parameters extracted from inspect."""
        from collections import Counter

        app = SmarterMCP(name="class-params-test")
        app.discover_module(Counter, include=["most_common"])
        tools = app._registry.get_all_tools()
        target = next((t for t in tools if "most_common" in t.name), None)
        assert target is not None, "most_common tool not found"
        eo = target.extracted_obj
        params = eo.parameters if eo else []
        # most_common(n=None) has one param 'n'
        assert len(params) >= 1, (
            f"Expected parameters for most_common, got {params!r}"
        )

    def test_local_class_discover_module(self, tmp_path, monkeypatch):
        """discover_module on a user-defined class in a real module works."""
        mod_name = "c4b_local_tools"
        mod_file = tmp_path / f"{mod_name}.py"
        mod_file.write_text(
            "class Calculator:\n"
            "    def add(self, a: int, b: int) -> int:\n"
            "        return a + b\n"
            "    def sub(self, a: int, b: int) -> int:\n"
            "        return a - b\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))

        import importlib
        mod = importlib.import_module(mod_name)
        cls = mod.Calculator

        app = SmarterMCP(name="cls-local")
        app.discover_module(cls, include=["add"])
        tools = app._registry.get_all_tools()
        assert any("add" in t.name for t in tools), (
            f"Expected 'add' tool, got {[t.name for t in tools]}"
        )
        for t in tools:
            if "add" in t.name:
                assert t.class_name == "Calculator"
