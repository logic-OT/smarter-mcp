"""
Type Coercion Engine.

Transforms FastMCP string inputs (which often come from LLMs as strings
even if the schema says integer) into actual Python objects that the underlying
methods expect.

Shared infrastructure (bracket-aware splitting, multimodal predicate, union
unwrapping) lives in ``smarter_mcp._typeparse`` and is imported here to keep
a single parser implementation across schema generation and coercion.
"""

from __future__ import annotations

import inspect
import json
import logging
from datetime import date, datetime
from typing import Any

from smarter_mcp._registry import RegisteredTool
from smarter_mcp._typeparse import is_multimodal_type, split_top_level, union_members
from smarter_mcp.errors import CoercionError

logger = logging.getLogger(__name__)

# Maximum number of bytes accepted for JSON string inputs before parsing.
# Prevents unbounded json.loads calls on adversarially large payloads (M19).
_MAX_JSON_INPUT_BYTES = 1 * 1024 * 1024  # 1 MiB

_NONE_NAMES = frozenset({"None", "NoneType"})

# Accepted string representations for bool coercion (case-insensitive).
_BOOL_TRUE_SET = frozenset({"true", "1", "yes", "on", "t", "y"})
_BOOL_FALSE_SET = frozenset({"false", "0", "no", "off", "f", "n"})


def coerce_arguments(
    tool: RegisteredTool,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Coerce FastMCP kwargs into the types expected by the callable.

    Args:
        tool: The registered tool.
        kwargs: The raw kwargs from FastMCP/JSON.

    Returns:
        A new dict with coerced values.
    """
    coerced = {}

    if tool.extracted_obj:
        for param in tool.extracted_obj.non_self_params:
            if param.is_variadic:
                continue

            if param.name not in kwargs:
                continue

            raw_val = kwargs[param.name]
            coerced[param.name] = _coerce_value_from_str(raw_val, param.effective_type, param.name)
    else:
        sig = inspect.signature(tool.fn)
        for name, param in sig.parameters.items():
            if name in ("self", "cls") or param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if name not in kwargs:
                continue

            raw_val = kwargs[name]
            type_str = None
            if param.annotation != inspect.Parameter.empty:
                if hasattr(param.annotation, "__name__"):
                    type_str = param.annotation.__name__
                else:
                    type_str = str(param.annotation)

            coerced[name] = _coerce_value_from_str(raw_val, type_str, name)

    # Pass through any unexpected kwargs
    for k, v in kwargs.items():
        if k not in coerced:
            coerced[k] = v

    return coerced


def _coerce_value_from_str(val: Any, type_str: str | None, param_name: str) -> Any:
    """Coerce a single value to the type named by *type_str* (best effort)."""
    if val is None:
        return None
    if not type_str:
        return val

    # Unwrap Optional[T] / Union[...] / "T | None" before scalar handling.
    # union_members() returns None when type_str is NOT a union at the top
    # level (e.g. "list[int | None]" — the | is bracket-nested) so we fall
    # through to scalar coercion and handle the list as a list (H9 fix).
    members = union_members(type_str)
    if members is not None:
        non_none = [m for m in members if m not in _NONE_NAMES]
        if not non_none:
            return val
        if len(non_none) == 1:
            return _coerce_value_from_str(val, non_none[0], param_name)
        # Multiple alternatives: the first that coerces cleanly wins.
        last_err: CoercionError | None = None
        for member in non_none:
            try:
                return _coerce_value_from_str(val, member, param_name)
            except CoercionError as e:
                last_err = e
        raise CoercionError(
            f"Value {val!r} for parameter '{param_name}' matched none of {non_none}: {last_err}"
        )

    return _coerce_scalar(val, type_str, param_name)


def _coerce_scalar(val: Any, type_str: str, param_name: str) -> Any:
    """Coerce to a single (non-union) type named by *type_str*."""
    # Strip module qualifier and generic args: "datetime.date" -> "date",
    # "list[int]" -> "list".
    simple = type_str.strip().split(".")[-1].split("[")[0]

    try:
        if simple == "int":
            return _coerce_int(val, type_str, param_name)

        if simple == "float":
            return float(val)

        if simple == "str":
            return str(val)

        if simple == "bool":
            return _coerce_bool(val, type_str, param_name)

        if simple in ("dict", "Dict"):
            if isinstance(val, str):
                _guard_json_size(val, type_str, param_name)
                return json.loads(val)
            if isinstance(val, dict):
                return val

        if simple in ("list", "List"):
            if isinstance(val, str):
                _guard_json_size(val, type_str, param_name)
                return json.loads(val)
            if isinstance(val, list):
                return val

        if simple == "datetime":
            return datetime.fromisoformat(val) if isinstance(val, str) else val

        if simple == "date":
            return date.fromisoformat(val) if isinstance(val, str) else val

    except CoercionError:
        raise
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        raise CoercionError(
            f"Cannot coerce {val!r} to '{type_str}' for parameter '{param_name}': {e}"
        ) from e

    # Multimodal image input coercion
    if is_multimodal_type(type_str):
        try:
            from smarter_mcp.multimodal.interceptor import resolve_image_input
            return resolve_image_input(val, type_str)
        except Exception as e:
            logger.error("Failed to resolve image input for '%s': %s", param_name, e)
            return val

    # Enum / Literal coercion is intentionally deferred: it needs the real
    # annotation object (not just its string name) plumbed through here.
    return val


# ---------------------------------------------------------------------------
# Scalar coercion helpers
# ---------------------------------------------------------------------------

def _coerce_int(val: Any, type_str: str, param_name: str) -> int:
    """Coerce *val* to int, rejecting non-integral floats and float-strings.

    Allowed: native int, integral float (3.0 → 3), integral float-string
    ("3.0" → 3), plain integer string ("42" → 42).
    Rejected: non-integral float (3.7), non-integral float-string ("3.7").
    """
    if isinstance(val, bool):
        # bool is a subclass of int; preserve as 0/1
        return int(val)

    if isinstance(val, float):
        if val != int(val):
            raise CoercionError(
                f"Cannot coerce non-integral float {val!r} to int "
                f"for parameter '{param_name}'"
            )
        return int(val)

    if isinstance(val, int):
        return val

    if isinstance(val, str):
        stripped = val.strip()
        # Try direct int conversion first ("42", "-7")
        try:
            return int(stripped)
        except ValueError:
            pass
        # Try float → int for integral float strings ("3.0", "42.0")
        try:
            f = float(stripped)
        except ValueError:
            raise CoercionError(
                f"Cannot coerce {val!r} to 'int' for parameter '{param_name}'"
            )
        if f != int(f):
            raise CoercionError(
                f"Cannot coerce non-integral float string {val!r} to int "
                f"for parameter '{param_name}'"
            )
        return int(f)

    # Last resort: try int() for other types (e.g. Decimal)
    return int(val)


def _coerce_bool(val: Any, type_str: str, param_name: str) -> bool:
    """Coerce *val* to bool with strict string validation.

    String inputs are accepted only if they are in the known true/false sets
    (case-insensitive).  Unrecognised strings raise ``CoercionError`` rather
    than silently mapping to ``False`` (M1a fix).
    """
    if isinstance(val, bool):
        return val

    if isinstance(val, int):
        return bool(val)

    if isinstance(val, str):
        lower = val.lower()
        if lower in _BOOL_TRUE_SET:
            return True
        if lower in _BOOL_FALSE_SET:
            return False
        raise CoercionError(
            f"Cannot coerce {val!r} to bool for parameter '{param_name}': "
            f"expected one of {sorted(_BOOL_TRUE_SET | _BOOL_FALSE_SET)}"
        )

    return bool(val)


def _guard_json_size(val: str, type_str: str, param_name: str) -> None:
    """Raise CoercionError if *val* exceeds the JSON input size limit (M19)."""
    size = len(val.encode("utf-8"))
    if size > _MAX_JSON_INPUT_BYTES:
        raise CoercionError(
            f"JSON input for parameter '{param_name}' (type '{type_str}') "
            f"exceeds the {_MAX_JSON_INPUT_BYTES // 1024} KiB size limit "
            f"({size} bytes received)"
        )
