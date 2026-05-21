"""
Schema endpoint.

Generates OpenAPI-compatible JSON schema for tools in a namespace.
"""

from __future__ import annotations

from typing import Any

from faster_mcp.extractor.models import ExtractionResult
from faster_mcp.server.router import _build_json_schema


class SchemaEndpoint:
    """Manages the JSON Schema generation endpoint."""

    def __init__(self, extraction: ExtractionResult):
        self.extraction = extraction

    def get_namespace_schema(self, namespace: str) -> dict[str, Any]:
        """Generate JSON Schema for a specific namespace.

        Returns:
            OpenAPI-compatible schema dictionary.
        """
        schema: dict[str, Any] = {
            "openapi": "3.1.0",
            "info": {
                "title": f"Namespace: {namespace}",
                "version": "1.0.0",
            },
            "paths": {},
            "components": {"schemas": {}},
        }

        # Find the module matching this namespace
        # (Very basic matching for now — assumes module_name matches namespace)
        module = None
        for m in self.extraction.modules:
            if m.module_name.split(".")[-1] == namespace or m.module_name.replace(".", "_") == namespace:
                module = m
                break

        if not module:
            return {"error": "Namespace not found"}

        for callable_obj in module.all_callables:
            # We skip properties (resources) for tool schemas
            if callable_obj.kind == "property":
                continue
                
            tool_name = callable_obj.simple_name
            if callable_obj.class_name:
                tool_name = f"{callable_obj.class_name}_{tool_name}"

            tool_schema = _build_json_schema(callable_obj)
            
            schema["paths"][f"/{tool_name}"] = {
                "post": {
                    "operationId": tool_name,
                    "description": callable_obj.docstring or "",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": tool_schema
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "Successful operation"
                        }
                    }
                }
            }

        return schema
