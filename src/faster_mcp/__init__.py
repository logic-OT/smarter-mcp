"""faster-mcp: Turn any Python codebase into a production-grade MCP server."""

from .server.app import FasterMCP
from ._decorators import tool, resource

__version__ = "0.1.0"
__all__ = ["FasterMCP", "tool", "resource"]
