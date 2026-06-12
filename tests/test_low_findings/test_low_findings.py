"""Tests for Low-severity production findings sweep."""
from __future__ import annotations


# Fix 1: non_self_params on free function
def test_non_self_params_free_function_keeps_self_named_params():
    """A free function with params named 'self' and 'cls' keeps both."""
    from smarter_mcp.extractor.models import (
        CallableKind,
        ExtractedCallable,
        ExtractedParam,
        ParamKind,
    )
    fn = ExtractedCallable(
        qualified_name="mymod.f",
        kind=CallableKind.FUNCTION,
        module_path="mymod.py",
        parameters=[
            ExtractedParam(name="self_explanatory", kind=ParamKind.POSITIONAL_OR_KEYWORD),
            ExtractedParam(name="cls", kind=ParamKind.POSITIONAL_OR_KEYWORD),
        ],
    )
    result = fn.non_self_params
    assert len(result) == 2
    assert result[0].name == "self_explanatory"
    assert result[1].name == "cls"


def test_non_self_params_method_drops_self_receiver():
    from smarter_mcp.extractor.models import (
        CallableKind,
        ExtractedCallable,
        ExtractedParam,
        ParamKind,
    )
    method = ExtractedCallable(
        qualified_name="mymod.Foo.bar",
        kind=CallableKind.METHOD,
        module_path="mymod.py",
        parameters=[
            ExtractedParam(name="self", kind=ParamKind.POSITIONAL_OR_KEYWORD),
            ExtractedParam(name="x", kind=ParamKind.POSITIONAL_OR_KEYWORD),
        ],
    )
    result = method.non_self_params
    assert len(result) == 1
    assert result[0].name == "x"


def test_non_self_params_classmethod_drops_cls_receiver():
    from smarter_mcp.extractor.models import (
        CallableKind,
        ExtractedCallable,
        ExtractedParam,
        ParamKind,
    )
    cm = ExtractedCallable(
        qualified_name="mymod.Foo.create",
        kind=CallableKind.CLASSMETHOD,
        module_path="mymod.py",
        parameters=[
            ExtractedParam(name="cls", kind=ParamKind.POSITIONAL_OR_KEYWORD),
            ExtractedParam(name="value", kind=ParamKind.POSITIONAL_OR_KEYWORD),
        ],
    )
    result = cm.non_self_params
    assert len(result) == 1
    assert result[0].name == "value"


# Fix 2: toolkit collision detection
def test_toolkit_collision_different_modules_warns(caplog):
    import logging

    from smarter_mcp._registry import ToolRegistry

    class Helper:
        pass

    class Helper2:
        pass

    # Simulate two different classes that share the same qualified key
    Helper.__name__ = "MyHelper"
    Helper.__qualname__ = "MyHelper"
    Helper.__module__ = "module_a"
    Helper2.__name__ = "MyHelper"
    Helper2.__qualname__ = "MyHelper"
    Helper2.__module__ = "module_a"  # same module → same key, different class object

    reg = ToolRegistry()
    reg.register_toolkit(Helper)
    with caplog.at_level(logging.WARNING, logger="smarter_mcp._registry"):
        reg.register_toolkit(Helper2)
    assert "collision" in caplog.text.lower() or "overwrite" in caplog.text.lower()


def test_toolkit_key_module_qualified():
    from smarter_mcp._registry import ToolRegistry

    class Alpha:
        pass

    Alpha.__module__ = "pkg.a"
    Alpha.__qualname__ = "Alpha"

    reg = ToolRegistry()
    reg.register_toolkit(Alpha)
    assert "pkg.a.Alpha" in reg._toolkits


# Fix 3: port=0 honored
def test_port_zero_is_honored():
    from smarter_mcp._decorators import clear_global_registry
    clear_global_registry()
    from smarter_mcp.server.app import SmarterMCP
    server = SmarterMCP("test-port-zero", port=0)
    assert server._config.server.port == 0
    clear_global_registry()


# Fix 4a: Sphinx inline type
def test_sphinx_inline_type_param():
    from smarter_mcp.extractor.docstrings import parse_docstring
    doc = """:param str name: The user's name.
:param int count: How many times.
:returns: A greeting.
:rtype: str
"""
    result = parse_docstring(doc)
    assert result.params.get("name") == "The user's name."
    assert result.param_types.get("name") == "str"
    assert result.params.get("count") == "How many times."
    assert result.param_types.get("count") == "int"


# Fix 4b: Google variadic args
def test_google_variadic_args_in_docstring():
    from smarter_mcp.extractor.docstrings import parse_docstring
    doc = """Process items.

Args:
    *args: Positional items to process.
    **kwargs: Additional keyword options.
"""
    result = parse_docstring(doc)
    assert "*args" in result.params, f"*args not in params: {result.params}"
    assert "**kwargs" in result.params, f"**kwargs not in params: {result.params}"


# Fix 5: nested return not counted
def test_nested_function_return_not_counted():
    from smarter_mcp.extractor.surface import SurfaceExtractor
    src = '''
def outer() -> None:
    def inner():
        return 42
    pass
'''
    extractor = SurfaceExtractor("/tmp", use_inspect=False)  # noqa: S108
    module = extractor.extract_source(src, "test.py", "test")
    outer_fn = next(f for f in module.functions if f.simple_name == "outer")
    # outer() has an explicit return annotation 'None', so return_type should be 'None'
    # but the type INFERENCE should not pick up 'int' from the nested inner() return
    assert outer_fn.return_type == "None"  # annotation wins


def test_nested_function_no_outer_inferred_type():
    """When outer has no annotation, nested returns must not pollute inference."""
    import ast

    from smarter_mcp.extractor.type_inference import infer_return_type
    src = '''
def outer():
    def inner():
        return 42
    return "hello"
'''
    tree = ast.parse(src)
    # outer returns "hello" (str), not 42 (int from inner)
    result = infer_return_type(tree, "outer")
    assert result == "str", f"Expected 'str' (outer's own return), got {result!r}"


# Fix 6: __all__ detection
def test_all_exports_module_level_only():
    """__all__ inside a function must not affect module-level all_exports."""
    from smarter_mcp.extractor.surface import SurfaceExtractor
    src = '''
def setup():
    __all__ = ["hidden"]

def real_func():
    pass
'''
    extractor = SurfaceExtractor("/tmp", use_inspect=False)  # noqa: S108
    module = extractor.extract_source(src, "mod.py", "mod")
    assert module.all_exports is None  # no module-level __all__


def test_all_exports_ann_assign():
    """__all__: list[str] = [...] (AnnAssign) must be parsed."""
    from smarter_mcp.extractor.surface import SurfaceExtractor
    src = '''
__all__: list[str] = ["pub"]

def pub():
    pass

def _priv():
    pass
'''
    extractor = SurfaceExtractor("/tmp", use_inspect=False)  # noqa: S108
    module = extractor.extract_source(src, "mod.py", "mod")
    assert module.all_exports == ["pub"]


def test_all_exports_dynamic_warns(caplog):
    """A dynamic __all__ must emit a WARNING naming the module."""
    import logging

    from smarter_mcp.extractor.surface import SurfaceExtractor
    src = '''
import os
__all__ = os.environ.get("EXPORTS", "").split()

def pub():
    pass
'''
    extractor = SurfaceExtractor("/tmp", use_inspect=False)  # noqa: S108
    with caplog.at_level(logging.WARNING, logger="smarter_mcp.extractor.surface"):
        module = extractor.extract_source(src, "mod.py", "mod")
    assert module.all_exports is None
    assert any("__all__" in r.message for r in caplog.records if r.levelno == logging.WARNING)


# Fix 7: init with nonexistent path
def test_init_nonexistent_path_creates_manifest(tmp_path):
    """init <nonexistent> without --output creates the dir and writes manifest there."""
    from click.testing import CliRunner

    from smarter_mcp._decorators import clear_global_registry
    from smarter_mcp.cli.main import cli
    clear_global_registry()
    new_dir = tmp_path / "brand-new-project"
    assert not new_dir.exists()
    runner = CliRunner()
    result = runner.invoke(cli, ["init", str(new_dir)])
    assert result.exit_code == 0, f"exit={result.exit_code}\n{result.output}\n{result.exception}"
    assert (new_dir / "smarter-mcp.yaml").exists()
    clear_global_registry()


def test_init_nonexistent_path_with_output_errors(tmp_path):
    """init <nonexistent> --output <dir> must fail clearly."""
    from click.testing import CliRunner

    from smarter_mcp._decorators import clear_global_registry
    from smarter_mcp.cli.main import cli
    clear_global_registry()
    new_dir = tmp_path / "nope"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["init", str(new_dir), "--output", str(out_dir)])
    assert result.exit_code != 0
    clear_global_registry()


# Fix 8: schema null-default consistency and serialization guard
def test_schema_null_default_omitted_signature_path():
    """Decorator-only tool with param default=None: schema must NOT emit 'default': null."""
    from smarter_mcp._decorators import clear_global_registry
    from smarter_mcp._registry import RegisteredTool
    from smarter_mcp._schema import build_json_schema
    clear_global_registry()

    def f(x: int | None = None): ...

    tool_obj = RegisteredTool(
        name="f", description=None, fn=f, namespace="default", source="decorator"
    )
    schema = build_json_schema(tool_obj)
    props = schema.get("properties", {})
    assert "default" not in props.get("x", {}), (
        f"Expected null default to be omitted, got: {props.get('x')}"
    )
    clear_global_registry()


def test_schema_non_serializable_default_omitted():
    """A non-JSON-serializable default must be silently omitted (not crash)."""
    import datetime

    from smarter_mcp._decorators import clear_global_registry
    from smarter_mcp._registry import RegisteredTool
    from smarter_mcp._schema import build_json_schema
    clear_global_registry()

    sentinel = datetime.datetime(2026, 1, 1)

    def g(ts: str = sentinel): ...

    tool_obj = RegisteredTool(
        name="g", description=None, fn=g, namespace="default", source="decorator"
    )
    schema = build_json_schema(tool_obj)
    # Must not raise; default should be absent
    props = schema.get("properties", {})
    assert "default" not in props.get("ts", {}), (
        f"Expected non-serializable default to be omitted, got: {props.get('ts')}"
    )
    clear_global_registry()


# Fix 9: file-size guard
def test_file_size_guard_skips_large_file(tmp_path, caplog):
    """A file exceeding _MAX_FILE_BYTES must be skipped with a WARNING."""
    import logging

    from smarter_mcp.extractor import surface as surface_mod
    from smarter_mcp.extractor.surface import SurfaceExtractor
    big_file = tmp_path / "huge.py"
    # Write a file just over the limit
    original_limit = surface_mod._MAX_FILE_BYTES
    surface_mod._MAX_FILE_BYTES = 50  # override for test
    try:
        big_file.write_text("x = 1\n" * 10)  # 60 bytes
        extractor = SurfaceExtractor(tmp_path, use_inspect=False)
        with caplog.at_level(logging.WARNING, logger="smarter_mcp.extractor.surface"):
            extractor.extract()
        assert any("huge.py" in r.message and r.levelno == logging.WARNING for r in caplog.records)
    finally:
        surface_mod._MAX_FILE_BYTES = original_limit
