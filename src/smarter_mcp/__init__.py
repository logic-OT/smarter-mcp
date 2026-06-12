"""smarter-mcp: Turn any Python codebase into a production-grade MCP server."""

from importlib.metadata import PackageNotFoundError, version

from ._decorators import resource, tool, toolkit
from .server.app import SmarterMCP

try:
    __version__: str = version("smarter-mcp")
except PackageNotFoundError:  # editable / src-layout install without metadata
    __version__ = "0.1.1"

__all__ = ["SmarterMCP", "__version__", "resource", "tool", "toolkit"]
