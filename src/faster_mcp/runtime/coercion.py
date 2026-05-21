"""
Type Coercion Engine.

Transforms FastMCP string inputs (which often come from LLMs as strings
even if the schema says integer) into actual Python objects that the underlying
methods expect.
"""

from __future__ import annotations

import json
import logging
from typing import Any, get_args, get_origin

import inspect
from faster_mcp._registry import RegisteredTool
from faster_mcp.extractor.models import ExtractedCallable, ExtractedParam

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


def _coerce_value_from_str(val: Any, type_str: str | None, param_name: str) -> Any:
    """Attempt to coerce a single value based on its type string."""
    if val is None:
        return None

    if not type_str:
        return val

    # Fast paths for simple types
    try:
        if type_str == "int":
            return int(val)
        if type_str == "float":
            return float(val)
        if type_str == "str":
            return str(val)
        if type_str == "bool":
            if isinstance(val, str):
                return val.lower() in ("true", "1", "yes", "t", "y")
            return bool(val)
        if type_str == "dict":
            if isinstance(val, str):
                return json.loads(val)
            if isinstance(val, dict):
                return val
        if type_str == "list":
            if isinstance(val, str):
                return json.loads(val)
            if isinstance(val, list):
                return val
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        logger.warning(
            "Failed to coerce %r to %s for parameter '%s': %s. "
            "Passing raw value.",
            val, type_str, param_name, e
        )
        return val

    return val
