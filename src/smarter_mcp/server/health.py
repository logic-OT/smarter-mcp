"""
Health endpoint definition.

Returns status, counts of namespaces/tools/resources, and version info.
Status is "degraded" when any extraction or import failures are present.

H8 security: unauthenticated callers receive only the bare ``{"status": ...}``
field so that the server surface (namespace list, tool counts, version) is not
disclosed without authentication.  Pass ``authenticated=True`` to get the full
detail payload.
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

    def get_health(self, *, authenticated: bool = False) -> dict[str, Any]:
        """Generate health status report.

        Args:
            authenticated: When True, include full detail (namespaces, counts,
                version).  When False (default, unauthenticated callers), return
                only ``{"status": ...}`` so the tool surface is not disclosed
                without valid credentials (H8).
        """
        extraction_errors = (
            len(self._extraction_result.errors)
            if self._extraction_result is not None
            else 0
        )
        import_failures = self._import_failure_count
        failed_modules = extraction_errors + import_failures

        status = "degraded" if failed_modules > 0 else "healthy"

        # Unauthenticated callers get the bare status only.
        if not authenticated:
            return {"status": status}

        # Authenticated callers get full detail.
        uptime = int(time.time() - self.start_time)
        namespaces = self.router.namespaces
        tool_count = len(self.registry.get_all_tools())
        resource_count = sum(
            len(self.registry.get_namespace_resources(ns))
            for ns in self.registry.get_all_namespaces()
        )

        return {
            "status": status,
            "uptime_seconds": uptime,
            "namespaces": namespaces,
            "tool_count": tool_count,
            "resource_count": resource_count,
            "extraction_errors": extraction_errors,
            "import_failures": import_failures,
            "failed_modules": failed_modules,
            "version": __version__,
        }
