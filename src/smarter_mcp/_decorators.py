from collections.abc import Callable
from typing import Any

# Valid lifecycle values for @toolkit — mirrors the Literal in manifest.InstanceConfig.
_VALID_LIFECYCLES: frozenset[str] = frozenset({"session", "singleton", "per-call"})

# Global registries
_GLOBAL_TOOLS: list[Callable] = []
_GLOBAL_RESOURCES: list[Callable] = []
_GLOBAL_TOOLKITS: list[type] = []

def register_global_tool(fn: Callable) -> None:
    if fn not in _GLOBAL_TOOLS:
        _GLOBAL_TOOLS.append(fn)

def register_global_resource(fn: Callable) -> None:
    if fn not in _GLOBAL_RESOURCES:
        _GLOBAL_RESOURCES.append(fn)

def register_global_toolkit(cls: type) -> None:
    if cls not in _GLOBAL_TOOLKITS:
        _GLOBAL_TOOLKITS.append(cls)

def get_global_tools() -> list[Callable]:
    return list(_GLOBAL_TOOLS)

def get_global_resources() -> list[Callable]:
    return list(_GLOBAL_RESOURCES)

def get_global_toolkits() -> list[type]:
    return list(_GLOBAL_TOOLKITS)

def clear_global_registry() -> None:
    _GLOBAL_TOOLS.clear()
    _GLOBAL_RESOURCES.clear()
    _GLOBAL_TOOLKITS.clear()


def tool(
    first_arg: Callable | str | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    tests: list[dict[str, Any]] | None = None
) -> Callable:
    """Mark a function or method as an MCP tool.

    Can be used with or without arguments:
        @tool
        @tool("Function description")
        @tool(name="custom_name", description="Desc")
    """
    resolved_description = description
    resolved_name = name
    fn_to_decorate = None

    if callable(first_arg):
        fn_to_decorate = first_arg
    elif isinstance(first_arg, str):
        resolved_description = first_arg

    def decorator(fn: Callable) -> Callable:
        fn._smarter_mcp_tool = True
        fn._smarter_mcp_name = resolved_name
        fn._smarter_mcp_description = resolved_description
        fn._smarter_mcp_tests = tests or []

        register_global_tool(fn)
        return fn

    if fn_to_decorate is not None:
        return decorator(fn_to_decorate)
    return decorator


def resource(
    first_arg: Callable | str | None = None,
    *,
    uri: str | None = None,
    description: str | None = None
) -> Callable:
    """Mark a function or method as an MCP resource.

    Can be used with or without arguments:
        @resource("resource://pattern")
        @resource(uri="resource://pattern", description="Desc")
    """
    resolved_uri = uri
    resolved_description = description
    fn_to_decorate = None

    if callable(first_arg):
        fn_to_decorate = first_arg
    elif isinstance(first_arg, str):
        resolved_uri = first_arg

    def decorator(fn: Callable) -> Callable:
        fn._smarter_mcp_resource = True
        fn._smarter_mcp_uri = resolved_uri
        fn._smarter_mcp_description = resolved_description

        register_global_resource(fn)
        return fn

    if fn_to_decorate is not None:
        return decorator(fn_to_decorate)
    return decorator


def toolkit(
    first_arg: type | str | None = None,
    *,
    lifecycle: str = "session",
    namespace: str = "default",
    constructor_args: dict[str, Any] | None = None
) -> Callable:
    """Mark a class as an MCP toolkit.

    Can be used with or without arguments:
        @toolkit
        @toolkit("namespace")
        @toolkit(namespace="my_namespace")
    """
    # M14: validate lifecycle at decoration time so typos are caught immediately,
    # not silently accepted and discovered only at first tool call.
    if lifecycle not in _VALID_LIFECYCLES:
        raise ValueError(
            f"Invalid lifecycle {lifecycle!r}. "
            f"Must be one of: {sorted(_VALID_LIFECYCLES)}"
        )

    resolved_namespace = namespace
    cls_to_decorate = None

    if isinstance(first_arg, type):
        cls_to_decorate = first_arg
    elif isinstance(first_arg, str):
        resolved_namespace = first_arg

    def decorator(cls: type) -> type:
        cls._smarter_mcp_toolkit = True
        cls._smarter_mcp_lifecycle = lifecycle
        cls._smarter_mcp_namespace = resolved_namespace
        cls._smarter_mcp_constructor_args = constructor_args or {}

        register_global_toolkit(cls)
        return cls

    if cls_to_decorate is not None:
        return decorator(cls_to_decorate)
    return decorator
