"""smarter-mcp: Turn any Python codebase into a production-grade MCP server."""

from .server.app import SmarterMCP
from ._decorators import tool, resource, toolkit

__version__ = "0.1.0"
__all__ = ["SmarterMCP", "tool", "resource", "toolkit"]
