"""Tests for NamespaceRouter._register_resource module-path resolution fallback.

Exercises the fix for server/router.py:253 where rstrip(".py") was used instead
of removesuffix(".py"), mangling module names (e.g. "app.py" → "a" instead of
"app").  The fallback fires when the resource's extracted_obj.qualified_name has
fewer than 3 dot-separated parts (i.e. no module prefix).
"""

from __future__ import annotations

import importlib

import pytest
from fastmcp import FastMCP

from smarter_mcp._decorators import clear_global_registry
from smarter_mcp._registry import RegisteredResource
from smarter_mcp.config.manifest import default_manifest
from smarter_mcp.extractor.models import CallableKind, ExtractedCallable
from smarter_mcp.runtime.instances import InstanceManager
from smarter_mcp.server.router import NamespaceRouter


@pytest.fixture(autouse=True)
def _reset():
    clear_global_registry()
    yield
    clear_global_registry()


class TestModulePathFallback:
    def test_rstrip_bug_fixed_for_app_py(self, tmp_path, monkeypatch):
        """module_path 'app.py' must resolve to module 'app', not 'a'.

        The rstrip(".py") bug strips the character SET {., p, y} from the right,
        turning "app.py" → "a".  removesuffix(".py") correctly gives "app".
        """
        (tmp_path / "app.py").write_text(
            "class Config:\n"
            "    @property\n"
            "    def version(self) -> str:\n"
            "        return 'v2'\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))

        # Track which module names are attempted
        attempted: list[str] = []
        orig_import = importlib.import_module

        def _spy_import(name: str, *a, **kw):
            attempted.append(name)
            return orig_import(name, *a, **kw)

        monkeypatch.setattr(importlib, "import_module", _spy_import)

        cfg = default_manifest()
        router = NamespaceRouter(config=cfg, instance_manager=InstanceManager([]))
        server = FastMCP("test")

        # Craft a resource whose qualified_name has only 2 parts → fallback fires
        ext = ExtractedCallable(
            qualified_name="Config.version",  # 2 parts — triggers module_path fallback
            kind=CallableKind.PROPERTY,
            module_path="app.py",             # the path that had the rstrip bug
            class_name="Config",
        )
        res = RegisteredResource(
            uri="resource://default/Config/version",
            description=None,
            fn=lambda: "v2",
            namespace="default",
            source="discovery",
            extracted_obj=ext,
        )
        router._register_resource(server, res, "default")

        # With rstrip bug:       "app.py".rstrip(".py") == "a"  → import "a"
        # With removesuffix fix: "app.py".removesuffix(".py") == "app" → import "app"
        assert "app" in attempted, (
            f"Expected 'app' to be imported from module_path='app.py'; "
            f"actual import attempts: {attempted}. "
            f"If 'a' appears, the rstrip() bug was not fixed."
        )
        assert "a" not in attempted, (
            f"'a' was imported — rstrip('.py') strips chars not suffix. "
            f"All import attempts: {attempted}"
        )

    def test_removesuffix_correct_for_subdir_path(self, tmp_path, monkeypatch):
        """module_path 'pkg/app.py' → module 'pkg.app' (not 'pkg.a')."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "app.py").write_text(
            "class Widget:\n"
            "    @property\n"
            "    def name(self) -> str:\n"
            "        return 'widget'\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))

        attempted: list[str] = []
        orig_import = importlib.import_module

        def _spy(name: str, *a, **kw):
            attempted.append(name)
            return orig_import(name, *a, **kw)

        monkeypatch.setattr(importlib, "import_module", _spy)

        cfg = default_manifest()
        router = NamespaceRouter(config=cfg, instance_manager=InstanceManager([]))
        server = FastMCP("test")

        ext = ExtractedCallable(
            qualified_name="Widget.name",     # 2 parts — triggers fallback
            kind=CallableKind.PROPERTY,
            module_path="pkg/app.py",         # subdir path
            class_name="Widget",
        )
        res = RegisteredResource(
            uri="resource://default/Widget/name",
            description=None,
            fn=lambda: "widget",
            namespace="default",
            source="discovery",
            extracted_obj=ext,
        )
        router._register_resource(server, res, "default")

        assert "pkg.app" in attempted, (
            f"Expected 'pkg.app' from module_path='pkg/app.py'; got: {attempted}"
        )
        assert "pkg.a" not in attempted, (
            f"'pkg.a' was attempted — rstrip bug present. Got: {attempted}"
        )
