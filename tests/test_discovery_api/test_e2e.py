"""End-to-end tests: C4 invocability, H13 property resources, H14 relative paths.

All MCP-wire tests (C4) call tools through the real in-memory FastMCP Client so
the full discover → build → dispatch path is exercised.  H13 and H14 are
integration tests at the registry / manifest layer.
"""

from __future__ import annotations

import pytest

from smarter_mcp import SmarterMCP
from smarter_mcp._decorators import clear_global_registry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_registry():
    """Isolate global decorator state between tests."""
    clear_global_registry()
    yield
    clear_global_registry()


def _tool_value(result) -> str | None:
    """Extract text from a FastMCP CallToolResult."""
    if hasattr(result, "data") and result.data is not None:
        return str(result.data)
    content = getattr(result, "content", None)
    if content:
        return getattr(content[0], "text", None)
    return str(result)


# ---------------------------------------------------------------------------
# C4 — end-to-end invocability: package path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c4_package_tool_invocable_via_client(tmp_path, monkeypatch):
    """After discover_module on a Python package (has __init__.py), calling
    the tool via the FastMCP Client must return the correct result."""
    from fastmcp import Client

    # Create a minimal package with a fully-annotated, non-variadic function.
    # (FastMCP rejects **kwargs tools, so we use a clean function rather than
    # a stdlib helper like json.dumps that has **kw.)
    pkg_dir = tmp_path / "mypkg_c4"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text(
        "def add_nums(a: int, b: int) -> int:\n    return a + b\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    import importlib
    pkg = importlib.import_module("mypkg_c4")

    app = SmarterMCP(name="c4-e2e-pkg")
    # discover_module on a package (has __init__.py → goes through package path)
    app.discover_module(pkg, include=["add_nums"])
    server = app.build()

    async with Client(server) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        # FastMCP prefixes tool names with the namespace ("mypkg_c4_")
        fn_name = next((n for n in tool_names if n.endswith("add_nums")), None)
        assert fn_name is not None, (
            f"Expected a tool ending with 'add_nums'; got {tool_names}"
        )

        res = await client.call_tool(fn_name, {"a": 3, "b": 4})
        value = _tool_value(res)
        # add_nums(3, 4) == 7
        assert value == "7", (
            f"Expected '7' for add_nums(3, 4); got {value!r}"
        )


# ---------------------------------------------------------------------------
# C4 — end-to-end invocability: class path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c4_class_tool_invocable_via_client(tmp_path, monkeypatch):
    """After discover_module on a class, calling the method tool via the
    FastMCP Client must return the correct result."""
    from fastmcp import Client

    mod_name = "c4e2e_calculator"
    mod_file = tmp_path / f"{mod_name}.py"
    mod_file.write_text(
        "class Calculator:\n"
        "    def add(self, a: int, b: int) -> int:\n"
        "        return a + b\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    import importlib
    mod = importlib.import_module(mod_name)
    cls = mod.Calculator

    app = SmarterMCP(name="c4-e2e-cls")
    app.discover_module(cls, include=["add"])
    server = app.build()

    async with Client(server) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        add_name = next((n for n in tool_names if "add" in n), None)
        assert add_name is not None, (
            f"Expected a tool containing 'add'; got {tool_names}"
        )

        res = await client.call_tool(add_name, {"a": 3, "b": 4})
        value = _tool_value(res)
        # Calculator().add(3, 4) == 7
        assert value == "7", (
            f"Expected '7' for Calculator.add(3, 4); got {value!r}"
        )


# ---------------------------------------------------------------------------
# H13 — property getter runs against an auto-bound instance via _bound_getter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_h13_property_resource_registered_and_invocable(tmp_path, monkeypatch):
    """discover() on a class with @property must register it as a resource whose
    getter is invoked against an auto-bound instance (H13 fix in router.py).

    This test FAILS if the _bound_getter wiring in NamespaceRouter._register_resource
    is removed: without it the raw fget (which requires 'self') is called with no
    arguments by FastMCP, raising TypeError.
    """
    from fastmcp import Client

    mod_name = "h13prop_mod"
    mod_file = tmp_path / f"{mod_name}.py"
    mod_file.write_text(
        "class Config:\n"
        "    @property\n"
        "    def version(self) -> str:\n"
        "        return '1.0'\n"
    )
    # Keep tmp_path in sys.path so the module is importable at build() time
    monkeypatch.syspath_prepend(str(tmp_path))

    app = SmarterMCP(name="h13-test", use_inspect=False)
    app.discover(str(tmp_path))

    # Verify registry metadata before building the server
    all_resources = []
    for ns in app._registry.get_all_namespaces():
        all_resources.extend(app._registry.get_namespace_resources(ns))

    version_res = next(
        (r for r in all_resources if "version" in r.uri),
        None,
    )
    assert version_res is not None, (
        f"Expected a resource for the 'version' property; "
        f"registered URIs: {[r.uri for r in all_resources]}"
    )
    assert version_res.extracted_obj is not None, (
        "Resource must carry extracted_obj so the router can bind it"
    )
    assert version_res.extracted_obj.class_name == "Config", (
        f"extracted_obj.class_name must be 'Config'; "
        f"got {version_res.extracted_obj.class_name!r}"
    )

    # build() wires _bound_getter: it wraps the raw fget so that calling the
    # resource with no arguments auto-instantiates Config and calls fget(instance).
    # Without _bound_getter, FastMCP calls fget() with no args → TypeError.
    server = app.build()
    async with Client(server) as client:
        # FastMCP may prefix the URI when mounting the sub-server, so discover
        # the actual serving URI by listing resources first.
        registered = await client.list_resources()
        version_uri = next(
            (str(r.uri) for r in registered if "version" in str(r.uri)),
            None,
        )
        assert version_uri is not None, (
            f"Expected a resource URI containing 'version'; got: "
            f"{[str(r.uri) for r in registered]}"
        )

        contents = await client.read_resource(version_uri)
        assert contents, "Expected at least one content item from the resource"
        text = getattr(contents[0], "text", None) or str(contents[0])
        assert text == "1.0", (
            f"Expected '1.0' from Config().version via H13 _bound_getter; "
            f"got {text!r}. If _bound_getter wiring is removed the raw fget "
            f"raises TypeError because 'self' is not passed."
        )


# ---------------------------------------------------------------------------
# H14 — relative paths in manifest resolve against manifest dir, not CWD
# ---------------------------------------------------------------------------

def test_h14_relative_path_resolves_to_manifest_dir(tmp_path, monkeypatch):
    """A manifest with sources.path='./tools' must resolve ./tools relative to
    the manifest file's directory, not the current working directory."""
    dir_a = tmp_path / "dir_a"
    tools_dir = dir_a / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "greet.py").write_text(
        "def greet(name: str) -> str:\n    return f'Hello, {name}!'\n"
    )

    manifest_path = dir_a / "smarter-mcp.yaml"
    manifest_path.write_text(
        "name: h14-test\n"
        "sources:\n"
        "  - path: ./tools\n"
    )

    # Invoke from a completely different CWD — the tools must still be found
    dir_b = tmp_path / "dir_b"
    dir_b.mkdir()
    monkeypatch.chdir(str(dir_b))

    app = SmarterMCP(manifest=str(manifest_path), use_inspect=False)
    app.build()

    tool_names = {t.name for t in app._registry.get_all_tools()}
    assert "greet" in tool_names, (
        f"Expected 'greet' from {tools_dir} (manifest_dir={dir_a}, "
        f"CWD={dir_b}); got {tool_names}. "
        f"Relative path './tools' was not resolved against the manifest dir."
    )
