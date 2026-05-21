"""
JSON Schema builder for MCP tools.

Builds JSON Schema from either:
1. ExtractedCallable metadata (AST-extracted tools)
2. inspect.signature fallback (decorator-registered tools with no AST metadata)

Used by both the NamespaceRouter (for FastMCP registration) and the
ToolTestRunner (for schema validation checks).
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from faster_mcp._registry import RegisteredTool


# Maps Python type annotation strings to JSON Schema type names.
_TYPE_MAP: dict[str, str] = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
    "None": "null",
    "bytes": "string",
}


def build_json_schema(tool: RegisteredTool) -> dict[str, Any]:
    """Build a JSON Schema for a tool's input parameters.

    Uses the ExtractedCallable metadata if available (AST-extracted tools),
    otherwise falls back to inspect.signature (decorator-registered tools).

    Args:
        tool: The registered tool to build a schema for.

    Returns:
        A JSON Schema dict describing the tool's input parameters.
    """
    # Path 1: We have AST metadata — use ExtractedCallable's rich param info
    if tool.extracted_obj:
        return _schema_from_extracted(tool)

    # Path 2: No AST metadata — introspect the live function signature
    return _schema_from_signature(tool.fn)


def _schema_from_extracted(tool: RegisteredTool) -> dict[str, Any]:
    """Build schema from ExtractedCallable metadata (AST-extracted tools)."""
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param in tool.extracted_obj.non_self_params:  # type: ignore[union-attr]
        if param.is_variadic:
            continue

        prop: dict[str, Any] = {}

        # Resolve the JSON type from the Python type annotation
        effective_type = param.effective_type
        if effective_type:
            # Strip generics and unions to get the base type name
            base_type = effective_type.split("[")[0].split("|")[0].strip()
            prop["type"] = _TYPE_MAP.get(base_type, "string")
        else:
            prop["type"] = "string"

        # Per-parameter description from docstring parsing
        if param.description:
            prop["description"] = param.description

        # Default value
        if param.has_default and param.default is not None:
            prop["default"] = param.default

        properties[param.name] = prop

        # Parameters without defaults are required
        if not param.has_default:
            required.append(param.name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required

    return schema


def _schema_from_signature(fn: Callable) -> dict[str, Any]:
    """Build schema by introspecting a live function's signature.

    This is the fallback path for decorator-registered tools that
    were never processed by the AST extractor.
    """
    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        # Skip self, cls, *args, **kwargs
        if name in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue

        prop: dict[str, Any] = {}

        # Resolve type annotation → JSON Schema type
        if param.annotation != inspect.Parameter.empty:
            type_name = (
                param.annotation.__name__
                if hasattr(param.annotation, "__name__")
                else str(param.annotation)
            )
            prop["type"] = _TYPE_MAP.get(type_name, "string")
        else:
            prop["type"] = "string"

        # Default value
        if param.default != inspect.Parameter.empty:
            prop["default"] = param.default
        else:
            required.append(name)

        properties[name] = prop

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required

    return schema
