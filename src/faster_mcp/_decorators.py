from typing import Any, Callable

def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    tests: list[dict[str, Any]] | None = None
) -> Callable:
    """Mark a function or method as an MCP tool."""
    def decorator(fn: Callable) -> Callable:
        fn._faster_mcp_tool = True
        fn._faster_mcp_name = name
        fn._faster_mcp_description = description
        fn._faster_mcp_tests = tests or []
        return fn
    return decorator

def resource(
    uri: str,
    *,
    description: str | None = None
) -> Callable:
    """Mark a function or method as an MCP resource."""
    def decorator(fn: Callable) -> Callable:
        fn._faster_mcp_resource = True
        fn._faster_mcp_uri = uri
        fn._faster_mcp_description = description
        return fn
    return decorator
