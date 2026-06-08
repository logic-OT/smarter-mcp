"""
Health endpoint definition.

Returns status, counts of namespaces/tools/resources, and version info.
"""

from __future__ import annotations

import time
from typing import Any

from smarter_mcp import __version__
from smarter_mcp._registry import ToolRegistry
from smarter_mcp.server.router import NamespaceRouter


class HealthEndpoint:
    """Manages the health check endpoint."""

    def __init__(self, router: NamespaceRouter, registry: ToolRegistry):
        self.router = router
        self.registry = registry
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

        return {
            "status": "healthy",
            "uptime_seconds": uptime,
            "namespaces": namespaces,
            "tool_count": tool_count,
            "resource_count": resource_count,
            "version": __version__,
        }
