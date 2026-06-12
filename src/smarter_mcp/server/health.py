"""
Health endpoint definition.

Returns status, counts of namespaces/tools/resources, and version info.
Status is "degraded" when any extraction or import failures are present.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from smarter_mcp import __version__
from smarter_mcp._registry import ToolRegistry
from smarter_mcp.server.router import NamespaceRouter

if TYPE_CHECKING:
    from smarter_mcp.extractor.models import ExtractionResult


class HealthEndpoint:
    """Manages the health check endpoint."""

    def __init__(
        self,
        router: NamespaceRouter,
        registry: ToolRegistry,
        extraction_result: ExtractionResult | None = None,
        import_failure_count: int = 0,
    ):
        self.router = router
        self.registry = registry
        self._extraction_result = extraction_result
        self._import_failure_count = import_failure_count
        self.start_time = time.time()

    def get_health(self) -> dict[str, Any]:
        """Generate health status report."""
        uptime = int(time.time() - self.start_time)

        namespaces = self.router.namespaces
        tool_count = len(self.registry.get_all_tools())
        resource_count = sum(
            len(self.registry.get_namespace_resources(ns))
            for ns in self.registry.get_all_namespaces()
        )

        extraction_errors = (
            len(self._extraction_result.errors)
            if self._extraction_result is not None
            else 0
        )
        # failed_modules covers both AST-parse failures and import failures.
        failed_modules = extraction_errors + self._import_failure_count

        status = "degraded" if (extraction_errors > 0 or failed_modules > 0) else "healthy"

        return {
            "status": status,
            "uptime_seconds": uptime,
            "namespaces": namespaces,
            "tool_count": tool_count,
            "resource_count": resource_count,
            "extraction_errors": extraction_errors,
            "failed_modules": failed_modules,
            "version": __version__,
        }
