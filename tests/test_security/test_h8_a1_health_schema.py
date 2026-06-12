"""Tests for H8 and A1 — health and schema endpoint security.

H8:
- Unauthenticated /health returns only {"status": ...}.
- Authenticated /health returns full detail.

A1:
- Tools with expose=False must not appear in /schema.
- get_namespace_schema returns {"error": ...} for missing namespace.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from smarter_mcp._registry import ToolRegistry
from smarter_mcp.extractor.models import ExtractionResult
from smarter_mcp.server.health import HealthEndpoint
from smarter_mcp.server.router import NamespaceRouter
from smarter_mcp.server.schema_endpoint import SchemaEndpoint


class TestH8HealthAuthGating:
    def _make_ep(self, extraction_errors: int = 0) -> HealthEndpoint:
        router = MagicMock(spec=NamespaceRouter)
        router.namespaces = ["tools", "db"]
        registry = ToolRegistry()
        extraction = ExtractionResult(
            errors=[f"err{i}" for i in range(extraction_errors)]
        )
        return HealthEndpoint(
            router=router,
            registry=registry,
            extraction_result=extraction,
        )

    def test_unauthenticated_returns_only_status(self):
        ep = self._make_ep()
        health = ep.get_health(authenticated=False)
        assert set(health.keys()) == {"status"}, (
            f"Unauthenticated health must only expose 'status', got keys: {set(health.keys())}"
        )

    def test_unauthenticated_correct_status_healthy(self):
        health = self._make_ep().get_health(authenticated=False)
        assert health["status"] == "healthy"

    def test_unauthenticated_correct_status_degraded(self):
        ep = self._make_ep(extraction_errors=1)
        health = ep.get_health(authenticated=False)
        assert health["status"] == "degraded"

    def test_authenticated_exposes_full_detail(self):
        ep = self._make_ep()
        health = ep.get_health(authenticated=True)
        expected_keys = {
            "status", "uptime_seconds", "namespaces", "tool_count",
            "resource_count", "extraction_errors", "import_failures",
            "failed_modules", "version",
        }
        assert expected_keys.issubset(set(health.keys())), (
            f"Authenticated health is missing keys: {expected_keys - set(health.keys())}"
        )

    def test_authenticated_namespaces_present(self):
        ep = self._make_ep()
        health = ep.get_health(authenticated=True)
        assert health["namespaces"] == ["tools", "db"]


class TestH8SchemaNotFound:
    def test_missing_namespace_returns_error_dict(self):
        registry = ToolRegistry()
        ep = SchemaEndpoint(registry)
        result = ep.get_namespace_schema("nonexistent")
        assert "error" in result, (
            "Missing namespace must return an error dict (caller maps to 404)"
        )


class TestA1ExposeFilter:
    """Tools with expose=False must be absent from /schema."""

    def test_exposed_tool_in_schema(self):
        """A normal tool must appear in the schema."""
        from smarter_mcp import SmarterMCP, tool
        from smarter_mcp._decorators import clear_global_registry

        clear_global_registry()
        try:
            @tool("Visible tool")
            def visible_fn(x: int) -> str:
                return str(x)

            app = SmarterMCP(name="test-a1-exposed")
            app.build()

            schema_ep = SchemaEndpoint(app._registry, router=app._router)
            result = schema_ep.get_namespace_schema("default")

            # Should not be an error
            assert "error" not in result, f"Expected schema, got: {result}"
        finally:
            clear_global_registry()

    def test_hidden_tool_not_in_schema(self, tmp_path):
        """A tool with expose=False in manifest must not appear in schema output."""
        import yaml

        from smarter_mcp import SmarterMCP, tool
        from smarter_mcp._decorators import clear_global_registry

        clear_global_registry()
        try:
            @tool("Hidden tool")
            def secret_tool(token: str) -> str:
                return token

            # Write a manifest that hides secret_tool
            manifest_path = tmp_path / "smarter-mcp.yaml"
            manifest_data = {
                "name": "test-a1-hidden",
                "tools": [{"function": "secret_tool", "expose": False}],
            }
            manifest_path.write_text(yaml.safe_dump(manifest_data))

            app = SmarterMCP(name="test-a1-hidden", manifest=str(manifest_path))
            app.build()

            schema_ep = SchemaEndpoint(app._registry, router=app._router)
            result = schema_ep.get_namespace_schema("default")

            # The namespace has no tools after filtering, so result should be an
            # error dict — which the route handler maps to HTTP 404.
            # OR the tool name must not appear in the schema output.
            if "error" not in result:
                paths = result.get("paths", {})
                tool_names_in_schema = list(paths.keys())
                assert not any(
                    "secret_tool" in name for name in tool_names_in_schema
                ), (
                    f"secret_tool must be hidden from schema but found: "
                    f"{tool_names_in_schema}"
                )
        finally:
            clear_global_registry()
