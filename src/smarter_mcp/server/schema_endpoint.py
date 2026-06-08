"""
Schema endpoint.

Generates OpenAPI-compatible JSON schema for tools in a namespace.
Supports a compact mode (?compact=true) that returns only tool names and
parameter names — useful when there are many tools and full schemas are too verbose.
"""

from __future__ import annotations

from typing import Any

from smarter_mcp._registry import ToolRegistry
from smarter_mcp._schema import build_json_schema


class SchemaEndpoint:
    """Manages the JSON Schema generation endpoint."""

    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def get_namespace_schema(self, namespace: str, compact: bool = False) -> dict[str, Any]:
        """Generate schema for all tools in a namespace.

        Args:
            namespace: The namespace to generate schema for.
            compact: If True, return only tool names and parameter names instead
                     of the full OpenAPI structure.

        Returns:
            Full OpenAPI 3.1 dict, or a compact summary when compact=True.
            Returns {"error": ...} if the namespace does not exist.
        """
        tools = self.registry.get_namespace_tools(namespace)
        if not tools:
            return {"error": f"Namespace '{namespace}' not found"}

        if compact:
            return {
                "namespace": namespace,
                "tools": [
                    {
                        "name": t.name,
                        "params": list(build_json_schema(t).get("properties", {}).keys()),
                    }
                    for t in tools
                ],
            }

        schema: dict[str, Any] = {
            "openapi": "3.1.0",
            "info": {
                "title": f"Namespace: {namespace}",
                "version": "1.0.0",
            },
            "paths": {},
            "components": {"schemas": {}},
        }

        for tool in tools:
            tool_schema = build_json_schema(tool)
            schema["paths"][f"/{tool.name}"] = {
                "post": {
                    "operationId": tool.name,
                    "description": tool.description or "",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": tool_schema,
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Successful operation"},
                    },
                }
            }

        return schema
