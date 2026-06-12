"""
Schema endpoint.

Generates OpenAPI-compatible JSON schema for tools in a namespace.
Supports a compact mode (?compact=true) that returns only tool names and
parameter names — useful when there are many tools and full schemas are too
verbose.

A1 security fix: schema is built from the ROUTER-registered tool surface, not
the raw registry.  This means:
- Tools with ``ToolOverride(expose=False)`` are absent.
- Renamed/suppressed tools use their final registered names.
- Namespaces not visible in the router return a 404 (not a 200 with an error
  key, which agents cannot distinguish from success).

H8 / A1: ``get_namespace_schema`` returns ``{"error": ...}`` for missing/suppressed
namespaces, and the caller (``app.py``) maps that to HTTP 404.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from smarter_mcp._registry import ToolRegistry
from smarter_mcp._schema import build_json_schema

if TYPE_CHECKING:
    from smarter_mcp.server.router import NamespaceRouter


class SchemaEndpoint:
    """Manages the JSON Schema generation endpoint."""

    def __init__(
        self,
        registry: ToolRegistry,
        router: NamespaceRouter | None = None,
    ):
        self.registry = registry
        # A1: when a router is provided, use it to determine which namespaces
        # and tools are actually exposed (respects expose=False etc.).
        self.router = router

    def _get_tools_for_namespace(self, namespace: str):
        """Return only the tools that are registered in the router for this namespace.

        Falls back to the raw registry when no router is available (e.g. in
        unit tests that construct SchemaEndpoint directly).
        """
        if self.router is None:
            return self.registry.get_namespace_tools(namespace)

        # A1: the router's _namespaces dict maps namespace → FastMCP sub-server.
        # The sub-server's registered tools represent the *actual* exposed surface
        # after applying overrides and expose=False filtering.
        # We cross-reference with the registry to recover the RegisteredTool objects
        # (which have the full type/description metadata needed for JSON schema).
        sub_server = self.router._namespaces.get(namespace)
        if sub_server is None:
            return []

        # Collect names actually registered on the sub-server.
        try:
            registered_names: set[str] = {t.name for t in sub_server.get_tools()}
        except Exception:
            # Fallback to registry if FastMCP API changes.
            return self.registry.get_namespace_tools(namespace)

        # Return registry tools whose final registered name is in the router surface.
        registry_tools = self.registry.get_namespace_tools(namespace)
        return [t for t in registry_tools if t.name in registered_names]

    def get_namespace_schema(
        self, namespace: str, compact: bool = False
    ) -> dict[str, Any]:
        """Generate schema for all tools in a namespace.

        A1: builds from the router-registered tool surface so hidden/suppressed
        tools do not appear.

        Args:
            namespace: The namespace to generate schema for.
            compact: If True, return only tool names and parameter names instead
                     of the full OpenAPI structure.

        Returns:
            Full OpenAPI 3.1 dict, or a compact summary when compact=True.
            Returns ``{"error": ...}`` if the namespace does not exist or has no
            exposed tools (caller maps this to HTTP 404).
        """
        # A1: use the router-filtered tool list.
        tools = self._get_tools_for_namespace(namespace)
        if not tools:
            return {"error": f"Namespace '{namespace}' not found"}

        if compact:
            return {
                "namespace": namespace,
                "tools": [
                    {
                        "name": t.name,
                        "params": list(
                            build_json_schema(t).get("properties", {}).keys()
                        ),
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
