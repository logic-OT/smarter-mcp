"""Runtime core tests — C1, C2, H10, H20, M4, M5.

All MCP-wire tests call tools through the real in-memory FastMCP Client so
the full wrapper → coercion → FastMCP dispatch path is exercised.
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from smarter_mcp._decorators import clear_global_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_registry():
    """Isolate global decorator state for every test."""
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
# C1 — str/bytes/Path returns must not be coerced into images
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c1_string_return_not_coerced_to_image():
    """greet() returns a plain string; it must survive the wrapper unchanged."""
    import asyncio
    from fastmcp import Client
    from smarter_mcp import SmarterMCP, tool

    @tool("Greet a user")
    def greet(name: str) -> str:
        return f"Hello, {name}!"

    app = SmarterMCP(name="test-c1")
    server = app.build()

    async with Client(server) as client:
        res = await client.call_tool("default_greet", {"name": "Ada"})
        value = _tool_value(res)
        assert value == "Hello, Ada!", f"Expected 'Hello, Ada!' got {value!r}"


@pytest.mark.asyncio
async def test_c1_int_return_not_coerced():
    """An integer return value must pass through unchanged."""
    from fastmcp import Client
    from smarter_mcp import SmarterMCP, tool

    @tool("Add two numbers")
    def add(a: int, b: int) -> int:
        return a + b

    app = SmarterMCP(name="test-c1-int")
    server = app.build()

    async with Client(server) as client:
        res = await client.call_tool("default_add", {"a": 3, "b": 4})
        value = _tool_value(res)
        assert value == "7", f"Expected '7', got {value!r}"


@pytest.mark.asyncio
async def test_c1_async_string_return_not_coerced():
    """Async str-returning tools are also fixed."""
    from fastmcp import Client
    from smarter_mcp import SmarterMCP, tool

    @tool("Async echo")
    async def echo(msg: str) -> str:
        return msg

    app = SmarterMCP(name="test-c1-async")
    server = app.build()

    async with Client(server) as client:
        res = await client.call_tool("default_echo", {"msg": "ping"})
        value = _tool_value(res)
        assert value == "ping", f"Expected 'ping' got {value!r}"


# ---------------------------------------------------------------------------
# C2 — @toolkit(lifecycle="session") keeps one instance per session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c2_session_lifecycle_increments():
    """Two bump() calls in one session must return 1 then 2 (not 1, 1)."""
    from fastmcp import Client
    from smarter_mcp import SmarterMCP

    # Write to a real module so class resolution via import works.
    tmp = Path(tempfile.mkdtemp())
    sys.path.insert(0, str(tmp))
    mod_name = "c2_test_counter"
    (tmp / f"{mod_name}.py").write_text(
        "from smarter_mcp import tool, toolkit\n\n"
        "@toolkit(lifecycle='session')\n"
        "class Counter:\n"
        "    def __init__(self):\n"
        "        self.n = 0\n"
        "    @tool(name='bump')\n"
        "    def bump(self) -> int:\n"
        "        self.n += 1\n"
        "        return self.n\n"
    )
    try:
        importlib.import_module(mod_name)
        app = SmarterMCP(name="test-c2")
        server = app.build()

        async with Client(server) as client:
            names = [t.name for t in await client.list_tools()]
            bump_name = next((n for n in names if n.endswith("bump")), None)
            assert bump_name is not None, f"bump tool not registered; tools={names}"

            r1 = await client.call_tool(bump_name, {})
            r2 = await client.call_tool(bump_name, {})
            v1, v2 = _tool_value(r1), _tool_value(r2)
            assert v2 in ("2", "2.0"), (
                f"Session lifecycle degraded to per-call: two bumps returned "
                f"{v1!r} then {v2!r} (expected 1 then 2)"
            )
    finally:
        sys.path.remove(str(tmp))
        sys.modules.pop(mod_name, None)


# ---------------------------------------------------------------------------
# H10 — static methods and classmethods work as tools
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_h10_staticmethod_tool():
    """A @staticmethod tool must be callable end-to-end."""
    from fastmcp import Client
    from smarter_mcp import SmarterMCP

    tmp = Path(tempfile.mkdtemp())
    sys.path.insert(0, str(tmp))
    mod_name = "h10_static_tools"
    (tmp / f"{mod_name}.py").write_text(
        "class MathTools:\n"
        "    @staticmethod\n"
        "    def double(x: int) -> int:\n"
        "        'Double a number.'\n"
        "        return x * 2\n"
    )
    try:
        mod = importlib.import_module(mod_name)
        app = SmarterMCP(name="test-h10-static")
        app.discover_module(mod)
        server = app.build()

        async with Client(server) as client:
            names = [t.name for t in await client.list_tools()]
            double_name = next((n for n in names if "double" in n), None)
            assert double_name is not None, f"double tool not found; tools={names}"

            res = await client.call_tool(double_name, {"x": 7})
            value = _tool_value(res)
            assert value == "14", f"Expected '14' got {value!r}"
    finally:
        sys.path.remove(str(tmp))
        sys.modules.pop(mod_name, None)


@pytest.mark.asyncio
async def test_h10_classmethod_tool():
    """A @classmethod tool must be callable end-to-end."""
    from fastmcp import Client
    from smarter_mcp import SmarterMCP

    tmp = Path(tempfile.mkdtemp())
    sys.path.insert(0, str(tmp))
    mod_name = "h10_class_tools"
    (tmp / f"{mod_name}.py").write_text(
        "class Factory:\n"
        "    prefix = 'item'\n"
        "    @classmethod\n"
        "    def make_label(cls, n: int) -> str:\n"
        "        'Create a label string.'\n"
        "        return f'{cls.prefix}_{n}'\n"
    )
    try:
        mod = importlib.import_module(mod_name)
        app = SmarterMCP(name="test-h10-cls")
        app.discover_module(mod)
        server = app.build()

        async with Client(server) as client:
            names = [t.name for t in await client.list_tools()]
            label_name = next((n for n in names if "make_label" in n), None)
            assert label_name is not None, f"make_label tool not found; tools={names}"

            res = await client.call_tool(label_name, {"n": 42})
            value = _tool_value(res)
            assert value == "item_42", f"Expected 'item_42' got {value!r}"
    finally:
        sys.path.remove(str(tmp))
        sys.modules.pop(mod_name, None)


# ---------------------------------------------------------------------------
# M5 — Context injection works even when param is not named 'ctx'
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_m5_context_param_not_named_ctx():
    """A tool with `context: Context` (not `ctx`) must not raise TypeError."""
    from fastmcp import Client, Context
    from smarter_mcp import SmarterMCP, tool

    @tool("Returns request_id via context")
    def get_id(context: Context) -> str:
        # Just confirm context is injected (not None / TypeError)
        return "ok"

    app = SmarterMCP(name="test-m5")
    server = app.build()

    async with Client(server) as client:
        # Should not raise TypeError about unexpected keyword argument
        res = await client.call_tool("default_get_id", {})
        value = _tool_value(res)
        assert value == "ok", f"Expected 'ok' got {value!r}"


# ---------------------------------------------------------------------------
# H20 — InstanceManager check-then-set is thread-safe
# ---------------------------------------------------------------------------

def test_h20_singleton_thread_safe():
    """Singleton creation under concurrent access produces exactly one instance."""
    from smarter_mcp.runtime.instances import InstanceManager
    from smarter_mcp.config.manifest import InstanceConfig

    class _DB:
        instances_created = 0
        def __init__(self):
            _DB.instances_created += 1

    _DB.instances_created = 0
    mgr = InstanceManager([
        InstanceConfig(class_name="_DB", lifecycle="singleton")
    ])

    results = []
    errors = []

    def worker():
        try:
            inst = mgr.get_instance("_DB", _DB, ctx=None)
            results.append(id(inst))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors during concurrent access: {errors}"
    # All threads should get the same instance
    assert len(set(results)) == 1, (
        f"Expected 1 unique instance under concurrent access, got {len(set(results))}"
    )
    # Created exactly once
    assert _DB.instances_created == 1, (
        f"Expected 1 construction, got {_DB.instances_created}"
    )


def test_h20_session_instance_thread_safe():
    """Session-instance creation for the same session_id is idempotent under threads."""
    import types
    from smarter_mcp.runtime.instances import InstanceManager
    from smarter_mcp.config.manifest import InstanceConfig

    class _Conn:
        instances_created = 0
        def __init__(self):
            _Conn.instances_created += 1

    _Conn.instances_created = 0
    mgr = InstanceManager([
        InstanceConfig(class_name="_Conn", lifecycle="session")
    ])

    # Fabricate a minimal Context-like object with a stable session_id
    fake_ctx = types.SimpleNamespace(session_id="session-abc")
    results = []
    errors = []

    def worker():
        try:
            inst = mgr.get_instance("_Conn", _Conn, ctx=fake_ctx)
            results.append(id(inst))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors: {errors}"
    assert len(set(results)) == 1, (
        f"Expected 1 unique instance per session, got {len(set(results))}"
    )
    assert _Conn.instances_created == 1, (
        f"Expected 1 construction, got {_Conn.instances_created}"
    )


# ---------------------------------------------------------------------------
# M4 — build() is idempotent: test cases not duplicated on second call
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Eviction path — max_sessions param triggers LRU close outside the lock
# ---------------------------------------------------------------------------

def test_eviction_calls_close_on_oldest_session():
    """Creating max_sessions+1 distinct sessions evicts the oldest and calls close()."""
    import types
    from smarter_mcp.runtime.instances import InstanceManager
    from smarter_mcp.config.manifest import InstanceConfig

    close_calls: list[int] = []

    class _Tracked:
        def __init__(self) -> None:
            self._closed = False

        def close(self) -> None:
            close_calls.append(id(self))
            self._closed = True

    max_s = 2
    mgr = InstanceManager(
        [InstanceConfig(class_name="_Tracked", lifecycle="session")],
        max_sessions=max_s,
    )

    # Create max_sessions+1 distinct sessions; the oldest must be evicted.
    ctx_objects = [
        types.SimpleNamespace(session_id=f"evict-sess-{i}") for i in range(max_s + 1)
    ]
    instances = [mgr.get_instance("_Tracked", _Tracked, ctx=c) for c in ctx_objects]

    assert len(close_calls) >= 1, (
        f"Expected at least 1 close() call after {max_s + 1} sessions with "
        f"max_sessions={max_s}; got close_calls={close_calls}"
    )
    assert id(instances[0]) in close_calls, (
        f"Oldest instance (id={id(instances[0])}) was not closed; "
        f"close_calls={close_calls}"
    )


def test_id_ctx_fallback_emits_warning(caplog):
    """ctx with no session_id/client_id triggers a logger.warning naming the class."""
    import logging
    import types
    from smarter_mcp.runtime.instances import InstanceManager
    from smarter_mcp.config.manifest import InstanceConfig

    class _Plain:
        pass

    mgr = InstanceManager([InstanceConfig(class_name="_Plain", lifecycle="session")])
    ctx = types.SimpleNamespace()  # no session_id, no client_id

    with caplog.at_level(logging.WARNING, logger="smarter_mcp.runtime.instances"):
        inst = mgr.get_instance("_Plain", _Plain, ctx=ctx)

    assert inst is not None
    assert any("id(ctx)" in r.message for r in caplog.records), (
        f"Expected a warning mentioning id(ctx) fallback; "
        f"got records: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# M4 — build() is idempotent: test cases not duplicated on second call
# ---------------------------------------------------------------------------

def test_m4_build_idempotent_no_duplicate_tests():
    """Calling build() twice must not duplicate tool.tests entries."""
    import yaml
    import tempfile as tf
    from smarter_mcp import SmarterMCP

    # Write a source file and a manifest that wires a test to the tool
    with tf.TemporaryDirectory() as td:
        src = Path(td) / "calc.py"
        src.write_text("def square(x: int) -> int:\n    return x * x\n")
        manifest_path = Path(td) / "smarter-mcp.yaml"
        manifest_path.write_text(
            f"name: test-m4\n"
            f"sources:\n"
            f"  - path: {td}\n"
            f"tools:\n"
            f"  - function: square\n"
            f"    tests:\n"
            f"      - params: {{x: 3}}\n"
            f"        expected: 9\n"
        )
        app = SmarterMCP(manifest=str(manifest_path))
        app.build()
        tools_after_first = app._registry.get_all_tools()
        square_first = next((t for t in tools_after_first if t.name == "square"), None)
        assert square_first is not None, "square tool not found after first build()"
        count_first = len(square_first.tests)

        # Second build() must not extend tests again
        app.build()
        count_second = len(square_first.tests)
        assert count_second == count_first, (
            f"build() duplicated test cases: {count_first} after first call, "
            f"{count_second} after second call"
        )
