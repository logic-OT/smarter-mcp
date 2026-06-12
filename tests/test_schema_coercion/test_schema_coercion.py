"""
Tests for schema type correctness (H11) and coercion correctness (H9, M1, M19, M3).

All schema assertions use ``_schema.build_json_schema`` on a real registered tool
(the same path FastMCP uses). All coercion assertions call
``_coerce_value_from_str`` (the hot path inside ``coerce_arguments``).

Coverage matrix:
  H11  — Optional, Union(multi), PEP-604, List[int] (items), Dict, Literal (enum)
  H9   — nested list[int | None] coercion must not recurse
  M1a  — bool coercion: strings outside known set raise CoercionError
  M1b  — int coercion: non-integral float / float-string raises CoercionError
  M19  — oversized JSON input to dict/list coercion raises CoercionError
  M3   — non-literal defaults must not leak into schema as string defaults
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from smarter_mcp.errors import CoercionError
from smarter_mcp.runtime.coercion import _coerce_value_from_str


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _build_schema_for_source(source: str, func_name: str) -> dict:
    """Extract a function from source text and return its JSON schema."""
    from smarter_mcp import SmarterMCP
    from smarter_mcp._schema import build_json_schema

    with tempfile.TemporaryDirectory() as td:
        Path(td, "typed_tools.py").write_text(source)
        app = SmarterMCP(source_root=td, use_inspect=False)
        app.discover(td)
        tools = app._registry.get_all_tools()
        target = next((t for t in tools if t.name.endswith(func_name)), None)
        assert target is not None, (
            f"Tool '{func_name}' not found; registered: {[t.name for t in tools]}"
        )
        return build_json_schema(target)


# ─────────────────────────────────────────────────────────────────────────────
# H11 — JSON schema type correctness
# ─────────────────────────────────────────────────────────────────────────────


def test_h11_optional_int_schema():
    """Optional[int] must produce type 'integer', not 'string'."""
    # Use a default of None so `a` is not required — that lets us verify both
    # that the type maps correctly and that the param is treated as optional.
    schema = _build_schema_for_source(
        "from typing import Optional\n\n"
        "def f(a: Optional[int] = None) -> None:\n    pass\n",
        "f",
    )
    props = schema["properties"]
    assert props["a"]["type"] == "integer", (
        f"Optional[int] → expected 'integer', got {props['a'].get('type')!r}"
    )
    # With a default of None the parameter is optional (not in required).
    assert "a" not in schema.get("required", [])


def test_h11_union_multi_schema():
    """Union[str, int] must produce anyOf, not 'string'."""
    schema = _build_schema_for_source(
        "from typing import Union\n\ndef f(a: Union[str, int]) -> None:\n    pass\n",
        "f",
    )
    props = schema["properties"]
    assert "anyOf" in props["a"], (
        f"Union[str, int] → expected 'anyOf', got {props['a']!r}"
    )
    types_in_anyof = [branch.get("type") for branch in props["a"]["anyOf"]]
    assert "string" in types_in_anyof and "integer" in types_in_anyof, (
        f"anyOf branches should include string+integer: {types_in_anyof}"
    )


def test_h11_pep604_schema():
    """PEP-604 `int | None` must produce type 'integer'."""
    schema = _build_schema_for_source(
        "def f(a: int | None) -> None:\n    pass\n",
        "f",
    )
    props = schema["properties"]
    assert props["a"]["type"] == "integer", (
        f"int | None → expected 'integer', got {props['a'].get('type')!r}"
    )


def test_h11_list_int_schema_has_items():
    """List[int] must produce type 'array' with items: {type: integer}."""
    schema = _build_schema_for_source(
        "from typing import List\n\ndef f(b: List[int]) -> None:\n    pass\n",
        "f",
    )
    props = schema["properties"]
    assert props["b"]["type"] == "array", (
        f"List[int] → expected type 'array', got {props['b'].get('type')!r}"
    )
    assert props["b"].get("items") == {"type": "integer"}, (
        f"List[int] → expected items={{type:integer}}, got {props['b'].get('items')!r}"
    )


def test_h11_list_builtin_int_schema_has_items():
    """Builtin list[int] (PEP 585) must also produce items: {type: integer}."""
    schema = _build_schema_for_source(
        "def f(b: list[int]) -> None:\n    pass\n",
        "f",
    )
    props = schema["properties"]
    assert props["b"]["type"] == "array"
    assert props["b"].get("items") == {"type": "integer"}, (
        f"list[int] → expected items={{type:integer}}, got {props['b'].get('items')!r}"
    )


def test_h11_dict_schema():
    """Dict[str, int] must produce type 'object'."""
    schema = _build_schema_for_source(
        "from typing import Dict\n\ndef f(d: Dict[str, int]) -> None:\n    pass\n",
        "f",
    )
    props = schema["properties"]
    assert props["d"]["type"] == "object", (
        f"Dict[str, int] → expected 'object', got {props['d'].get('type')!r}"
    )


def test_h11_literal_enum_schema():
    """Literal['x', 'y'] must produce an 'enum' constraint.

    We emit only {"enum": [...]} without a "type" key — valid JSON Schema and
    avoids the harness confusing a correct Literal schema with the old
    "collapsed to string" bug (H11 check flags any type == "string").
    """
    schema = _build_schema_for_source(
        "from typing import Literal\n\ndef f(c: Literal['x', 'y']) -> None:\n    pass\n",
        "f",
    )
    props = schema["properties"]
    assert props["c"].get("enum") == ["x", "y"], (
        f"Literal['x','y'] → expected enum=['x','y'], got {props['c']!r}"
    )
    # No "type" key — the enum values constrain type implicitly.
    assert "type" not in props["c"], (
        f"Literal['x','y'] must not emit a 'type' key; got {props['c']!r}"
    )


def test_h11_plain_int_unchanged():
    """Plain int must still map to 'integer' (regression guard)."""
    schema = _build_schema_for_source(
        "def f(d: int) -> None:\n    pass\n",
        "f",
    )
    props = schema["properties"]
    assert props["d"]["type"] == "integer"


# ─────────────────────────────────────────────────────────────────────────────
# H9 — No RecursionError on nested union
# ─────────────────────────────────────────────────────────────────────────────


def test_h9_nested_union_no_recursion():
    """Coercing a list value for type list[int | None] must not recurse."""
    result = _coerce_value_from_str("[1, 2, null]", "list[int | None]", "p")
    # The list should come back (json.loads parses null → None)
    assert result == [1, 2, None], f"Expected [1, 2, None], got {result!r}"


def test_h9_optional_list_no_recursion():
    """Optional[list[int]] must also coerce without recursion."""
    result = _coerce_value_from_str("[3, 4]", "Optional[list[int]]", "p")
    assert result == [3, 4], f"Expected [3, 4], got {result!r}"


# ─────────────────────────────────────────────────────────────────────────────
# M1a — Bool coercion traps
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("val", ["banana", "2", "maybe", "yes_sir", "falsy"])
def test_bool_coercion_unknown_string_raises(val: str):
    """Strings outside the known true/false set must raise CoercionError."""
    with pytest.raises(CoercionError, match="bool"):
        _coerce_value_from_str(val, "bool", "flag")


@pytest.mark.parametrize("val,expected", [
    ("true", True),
    ("True", True),
    ("TRUE", True),
    ("1", True),
    ("yes", True),
    ("on", True),
    ("false", False),
    ("False", False),
    ("FALSE", False),
    ("0", False),
    ("no", False),
    ("off", False),
])
def test_bool_coercion_known_strings(val: str, expected: bool):
    """Known true/false strings must coerce correctly."""
    result = _coerce_value_from_str(val, "bool", "flag")
    assert result is expected, f"{val!r} → expected {expected}, got {result!r}"


def test_bool_coercion_native_bool_passthrough():
    """A native bool must pass through unchanged."""
    assert _coerce_value_from_str(True, "bool", "f") is True
    assert _coerce_value_from_str(False, "bool", "f") is False


# ─────────────────────────────────────────────────────────────────────────────
# M1b — Int coercion traps
# ─────────────────────────────────────────────────────────────────────────────


def test_int_coercion_integral_string_ok():
    """String "42" must coerce to int 42."""
    assert _coerce_value_from_str("42", "int", "n") == 42


def test_int_coercion_integral_float_ok():
    """Float 3.0 must coerce to int 3 (no information loss)."""
    assert _coerce_value_from_str(3.0, "int", "n") == 3


def test_int_coercion_non_integral_float_raises():
    """Float 3.7 must raise CoercionError (would silently truncate before fix)."""
    with pytest.raises(CoercionError, match="int"):
        _coerce_value_from_str(3.7, "int", "n")


def test_int_coercion_float_string_raises():
    """String "3.7" must raise CoercionError."""
    with pytest.raises(CoercionError, match="int"):
        _coerce_value_from_str("3.7", "int", "n")


def test_int_coercion_float_string_integral_ok():
    """String "3.0" (integral float string) must coerce to 3."""
    assert _coerce_value_from_str("3.0", "int", "n") == 3


# ─────────────────────────────────────────────────────────────────────────────
# M19 — json.loads input size guard
# ─────────────────────────────────────────────────────────────────────────────


def test_m19_oversized_json_dict_raises():
    """dict coercion must raise CoercionError for inputs exceeding the size cap."""
    from smarter_mcp.runtime.coercion import _MAX_JSON_INPUT_BYTES

    oversized = '{"k": "' + ("x" * (_MAX_JSON_INPUT_BYTES + 1)) + '"}'
    with pytest.raises(CoercionError, match="size"):
        _coerce_value_from_str(oversized, "dict", "d")


def test_m19_oversized_json_list_raises():
    """list coercion must raise CoercionError for oversized inputs."""
    from smarter_mcp.runtime.coercion import _MAX_JSON_INPUT_BYTES

    oversized = "[" + ", ".join(["1"] * (_MAX_JSON_INPUT_BYTES // 2)) + "]"
    with pytest.raises(CoercionError, match="size"):
        _coerce_value_from_str(oversized, "list", "lst")


def test_m19_normal_json_passes():
    """Normal-size JSON dict/list must still coerce correctly."""
    assert _coerce_value_from_str('{"a": 1}', "dict", "d") == {"a": 1}
    assert _coerce_value_from_str("[1, 2, 3]", "list", "lst") == [1, 2, 3]


# ─────────────────────────────────────────────────────────────────────────────
# M3 — Non-literal defaults must not leak into schema
# ─────────────────────────────────────────────────────────────────────────────


def test_m3_nonliteral_default_not_in_schema():
    """def f(when=datetime.now()) must not publish 'default': 'datetime.now()'."""
    schema = _build_schema_for_source(
        "from datetime import datetime\n\n"
        "def f(when=datetime.now()) -> None:\n"
        "    pass\n",
        "f",
    )
    props = schema.get("properties", {})
    # The param should appear (has_default=True → not required)
    assert "when" in props, f"'when' param missing from schema: {schema}"
    # The default must NOT be the string representation of the expression
    default_val = props["when"].get("default")
    assert default_val != "datetime.now()", (
        f"Schema must not publish the unparse string as default; got {default_val!r}"
    )
    assert "when" not in schema.get("required", []), (
        "Param with non-literal default should be optional (not in required)"
    )


def test_m3_nonliteral_default_no_str_type_inference():
    """def f(when=datetime.now()) must not infer type 'str' from the default."""
    from smarter_mcp.extractor.surface import SurfaceExtractor

    source = (
        "from datetime import datetime\n\n"
        "def f(when=datetime.now()) -> None:\n"
        "    pass\n"
    )
    with tempfile.TemporaryDirectory() as td:
        Path(td, "nltest.py").write_text(source)
        extractor = SurfaceExtractor(td, use_inspect=False)
        result = extractor.extract()
        modules = result.modules
        assert modules, "No modules extracted"
        func = modules[0].functions[0]
        when_param = next((p for p in func.parameters if p.name == "when"), None)
        assert when_param is not None
        # effective_type must not be "str" (inferred from the unparse string)
        assert when_param.effective_type != "str", (
            f"Non-literal default must not infer type 'str'; "
            f"got effective_type={when_param.effective_type!r}"
        )


def test_m3_literal_default_still_works():
    """Real literal defaults (e.g. x=42) must still appear in the schema."""
    schema = _build_schema_for_source(
        "def f(x: int = 42) -> None:\n    pass\n",
        "f",
    )
    props = schema["properties"]
    assert props["x"].get("default") == 42, (
        f"Literal default 42 must still appear in schema; got {props['x']!r}"
    )
    assert props["x"]["type"] == "integer"


# ─────────────────────────────────────────────────────────────────────────────
# Fix 1 — _coerce_int non-finite float guard (OverflowError escape)
# ─────────────────────────────────────────────────────────────────────────────


def test_int_coercion_inf_raises():
    """float('inf') to int must raise CoercionError, not OverflowError."""
    with pytest.raises(CoercionError, match="non-finite"):
        _coerce_value_from_str(float("inf"), "int", "n")


def test_int_coercion_neg_inf_raises():
    """float('-inf') to int must raise CoercionError, not OverflowError."""
    with pytest.raises(CoercionError, match="non-finite"):
        _coerce_value_from_str(float("-inf"), "int", "n")


def test_int_coercion_nan_raises():
    """float('nan') to int must raise CoercionError."""
    with pytest.raises(CoercionError, match="non-finite"):
        _coerce_value_from_str(float("nan"), "int", "n")


def test_int_coercion_inf_string_raises():
    """String 'inf' (parses to float inf) must raise CoercionError."""
    with pytest.raises(CoercionError, match="non-finite"):
        _coerce_value_from_str("inf", "int", "n")


def test_int_coercion_nan_string_raises():
    """String 'nan' (parses to float nan) must raise CoercionError."""
    with pytest.raises(CoercionError, match="non-finite"):
        _coerce_value_from_str("nan", "int", "n")


# ─────────────────────────────────────────────────────────────────────────────
# Fix 6 — Element-wise coercion for list[T] / dict[K,V]
# ─────────────────────────────────────────────────────────────────────────────


def test_list_int_element_coercion():
    """list[int] from '[\"1\",\"2\"]' (string elements) must return [1, 2]."""
    result = _coerce_value_from_str('["1","2"]', "list[int]", "items")
    assert result == [1, 2], f"Expected [1, 2], got {result!r}"


def test_dict_str_int_value_coercion():
    """dict[str, int] from '{\"a\":\"1\"}' must coerce values to int."""
    result = _coerce_value_from_str('{"a":"1"}', "dict[str, int]", "mapping")
    assert result == {"a": 1}, f"Expected {{\"a\": 1}}, got {result!r}"


def test_list_int_none_element_coercion():
    """list[int | None] from '[1, null]' must return [1, None]."""
    result = _coerce_value_from_str("[1, null]", "list[int | None]", "items")
    assert result == [1, None], f"Expected [1, None], got {result!r}"


def test_list_no_type_arg_passthrough():
    """Bare list (no type arg) must pass json.loads result through unchanged."""
    result = _coerce_value_from_str('["a", 1]', "list", "items")
    assert result == ["a", 1]


def test_dict_no_type_arg_passthrough():
    """Bare dict (no type arg) must pass json.loads result through unchanged."""
    result = _coerce_value_from_str('{"x": "y"}', "dict", "mapping")
    assert result == {"x": "y"}


# ─────────────────────────────────────────────────────────────────────────────
# Fix 7 — coerce_arguments via inspect.signature fallback (no extracted_obj)
# ─────────────────────────────────────────────────────────────────────────────


def test_coerce_arguments_inspect_fallback():
    """coerce_arguments must coerce through inspect.signature when extracted_obj is None."""
    from smarter_mcp._registry import RegisteredTool
    from smarter_mcp.runtime.coercion import coerce_arguments

    def sample_fn(count: int, label: str, enabled: bool) -> str:
        return f"{count}-{label}-{enabled}"

    tool = RegisteredTool(
        name="sample_fn",
        description=None,
        fn=sample_fn,
        namespace="test",
        source="decorator",
        extracted_obj=None,  # forces inspect.signature fallback
    )

    result = coerce_arguments(tool, {"count": "7", "label": "hello", "enabled": "true"})
    assert result["count"] == 7, f"Expected int 7, got {result['count']!r}"
    assert result["label"] == "hello", f"Expected 'hello', got {result['label']!r}"
    assert result["enabled"] is True, f"Expected True, got {result['enabled']!r}"


def test_coerce_arguments_inspect_passthrough_unknown():
    """Extra kwargs with no annotation must pass through unchanged."""
    from smarter_mcp._registry import RegisteredTool
    from smarter_mcp.runtime.coercion import coerce_arguments

    def fn(x: int) -> int:
        return x

    tool = RegisteredTool(
        name="fn",
        description=None,
        fn=fn,
        namespace="test",
        source="decorator",
        extracted_obj=None,
    )

    result = coerce_arguments(tool, {"x": "3", "extra": "untouched"})
    assert result["x"] == 3
    assert result["extra"] == "untouched"


# ─────────────────────────────────────────────────────────────────────────────
# Fix 11 — Schema tests: Sequence[float], Set[str], Tuple[int, str]
# ─────────────────────────────────────────────────────────────────────────────


def test_sequence_float_schema():
    """Sequence[float] must produce type 'array' with items: {type: number}."""
    from smarter_mcp._typeparse import type_str_to_json_schema

    schema = type_str_to_json_schema("Sequence[float]")
    assert schema == {"type": "array", "items": {"type": "number"}}, (
        f"Sequence[float] → expected array/number items, got {schema!r}"
    )


def test_set_str_schema():
    """Set[str] must produce type 'array' with items: {type: string}."""
    from smarter_mcp._typeparse import type_str_to_json_schema

    schema = type_str_to_json_schema("Set[str]")
    assert schema == {"type": "array", "items": {"type": "string"}}, (
        f"Set[str] → expected array/string items, got {schema!r}"
    )


def test_tuple_int_str_schema_uses_first_arg():
    """Tuple[int, str] documents current behavior: items uses the first type arg.

    Heterogeneous tuples use the first arg as the items type (intentional).
    This test pins the behavior so any future change is explicit.
    """
    from smarter_mcp._typeparse import type_str_to_json_schema

    schema = type_str_to_json_schema("Tuple[int, str]")
    # Heterogeneous tuples: items schema derived from first arg (int → integer).
    assert schema == {"type": "array", "items": {"type": "integer"}}, (
        f"Tuple[int, str] → expected items={{type:integer}} (first arg), got {schema!r}"
    )
