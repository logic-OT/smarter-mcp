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
from collections.abc import Callable
from typing import Any

from smarter_mcp._registry import RegisteredTool
from smarter_mcp._typeparse import is_multimodal_type, type_str_to_json_schema
from smarter_mcp.extractor.models import _NON_LITERAL_TYPE


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
            type_schema = type_str_to_json_schema(effective_type)
            prop.update(type_schema)
            multimodal = is_multimodal_type(effective_type)
        else:
            prop["type"] = "string"
            multimodal = False

        # Per-parameter description from docstring parsing
        if param.description:
            prop["description"] = param.description

        # Multimodal parameters: add a description hint for clients.
        # type_str_to_json_schema already returns {"type": "string"} for
        # multimodal types, so no type override is needed here.
        if multimodal:
            hint = "File path or remote URL to the image"
            existing_desc = prop.get("description", "")
            if existing_desc:
                if hint not in existing_desc:
                    prop["description"] = f"{existing_desc} ({hint})"
            else:
                prop["description"] = hint

        # Default value — skip NON_LITERAL and None sentinels, guard serializability
        if (
            param.has_default
            and param.default is not None
            and not isinstance(param.default, _NON_LITERAL_TYPE)
        ):
            try:
                import json as _json
                _json.dumps(param.default)
                prop["default"] = param.default
            except (TypeError, ValueError):
                pass  # Non-serializable default — omit rather than crash /schema

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
        multimodal = False
        if param.annotation != inspect.Parameter.empty:
            ann = param.annotation
            type_str = (
                ann.__name__
                if hasattr(ann, "__name__")
                else str(ann)
            )
            type_schema = type_str_to_json_schema(type_str)
            prop.update(type_schema)
            multimodal = is_multimodal_type(type_str)
        else:
            prop["type"] = "string"

        # type_str_to_json_schema already returns {"type": "string"} for
        # multimodal types, so no type override is needed here.
        if multimodal:
            hint = "File path or remote URL to the image"
            existing_desc = prop.get("description", "")
            if existing_desc:
                if hint not in existing_desc:
                    prop["description"] = f"{existing_desc} ({hint})"
            else:
                prop["description"] = hint

        # Default value — omit None defaults (consistent with _schema_from_extracted)
        # and guard against non-JSON-serializable values so /schema can never raise.
        if param.default != inspect.Parameter.empty:
            if param.default is not None:
                try:
                    import json as _json
                    _json.dumps(param.default)
                    prop["default"] = param.default
                except (TypeError, ValueError):
                    pass  # Non-serializable default — omit rather than crash /schema
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
