"""
Type Coercion Engine.

Transforms FastMCP string inputs (which often come from LLMs as strings
even if the schema says integer) into actual Python objects that the underlying
methods expect.
"""

from __future__ import annotations

import inspect
import json
import logging
from datetime import date, datetime
from typing import Any

from smarter_mcp._registry import RegisteredTool
from smarter_mcp.errors import CoercionError

logger = logging.getLogger(__name__)


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


_NONE_NAMES = frozenset({"None", "NoneType"})
_BOOL_TRUE = ("true", "1", "yes", "t", "y")


def _split_top_level(s: str, sep: str) -> list[str]:
    """Split on `sep`, ignoring separators nested inside [] or () brackets."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return parts


def _union_members(type_str: str) -> list[str] | None:
    """Return inner member type strings if `type_str` is Optional/Union/PEP-604.

    Returns None for non-union types (so the caller falls through to scalar
    coercion). `typing.` prefixes are stripped.
    """
    s = type_str.replace("typing.", "").strip()
    if s.startswith("Optional[") and s.endswith("]"):
        return _split_top_level(s[len("Optional["):-1], ",")
    if s.startswith("Union[") and s.endswith("]"):
        return _split_top_level(s[len("Union["):-1], ",")
    if "|" in s:  # PEP 604: "int | None"
        return _split_top_level(s, "|")
    return None


def _coerce_value_from_str(val: Any, type_str: str | None, param_name: str) -> Any:
    """Coerce a single value to the type named by `type_str` (best effort)."""
    if val is None:
        return None
    if not type_str:
        return val

    # Unwrap Optional[T] / Union[...] / "T | None" before scalar handling.
    members = _union_members(type_str)
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
    """Coerce to a single (non-union) type named by `type_str`."""
    # Strip module qualifier and generic args: "datetime.date" -> "date",
    # "list[int]" -> "list".
    simple = type_str.strip().split(".")[-1].split("[")[0]

    try:
        if simple == "int":
            return int(val)
        if simple == "float":
            return float(val)
        if simple == "str":
            return str(val)
        if simple == "bool":
            if isinstance(val, str):
                return val.lower() in _BOOL_TRUE
            return bool(val)
        if simple == "dict":
            if isinstance(val, str):
                return json.loads(val)
            if isinstance(val, dict):
                return val
        if simple == "list":
            if isinstance(val, str):
                return json.loads(val)
            if isinstance(val, list):
                return val
        if simple == "datetime":
            return datetime.fromisoformat(val) if isinstance(val, str) else val
        if simple == "date":
            return date.fromisoformat(val) if isinstance(val, str) else val
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        raise CoercionError(
            f"Cannot coerce {val!r} to '{type_str}' for parameter '{param_name}': {e}"
        ) from e

    # Multimodal image input coercion
    type_lower = type_str.lower()
    if "pil.image" in type_lower or "image.image" in type_lower or "ndarray" in type_lower or "numpy.ndarray" in type_lower or type_lower in ("image", "pil_image"):
        try:
            from smarter_mcp.multimodal.interceptor import resolve_image_input
            return resolve_image_input(val, type_str)
        except Exception as e:
            logger.error("Failed to resolve image input for '%s': %s", param_name, e)
            return val

    # Enum / Literal coercion is intentionally deferred: it needs the real
    # annotation object (not just its string name) plumbed through here.
    return val

