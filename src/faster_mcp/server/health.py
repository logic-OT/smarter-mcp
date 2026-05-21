"""
Health endpoint definition.

Returns status, counts of namespaces/tools/resources, and version info.
"""

from __future__ import annotations

import time
from typing import Any

from faster_mcp import __version__
from faster_mcp.server.router import NamespaceRouter


class HealthEndpoint:
    """Manages the health check endpoint."""

    def __init__(self, router: NamespaceRouter):
        self.router = router
        self.start_time = time.time()

    def get_health(self) -> dict[str, Any]:
        """Generate health status report."""
        uptime = int(time.time() - self.start_time)
        
        namespaces = self.router.namespaces
        tool_count = 0
        resource_count = 0
        
        if self.router.root_server:
            # Note: We count the actual registered tools in the FastMCP instance
            tool_count = len(self.router.root_server._tools)
            resource_count = len(self.router.root_server._resources)

        return {
            "status": "healthy",
            "uptime_seconds": uptime,
            "namespaces": namespaces,
            "tool_count": tool_count,
            "resource_count": resource_count,
            "version": __version__,
        }
