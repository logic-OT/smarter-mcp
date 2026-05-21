"""
Wrappers that adapt unbound callables into FastMCP tools.

These wrappers handle:
1. FastMCP Context injection.
2. Instance resolution (for class methods).
3. Type coercion (adapting FastMCP JSON inputs to Python types).
"""

from __future__ import annotations

import functools
import inspect
import logging
from typing import Any, Callable

from fastmcp import Context

from faster_mcp._registry import RegisteredTool
from faster_mcp.runtime.coercion import coerce_arguments
from faster_mcp.runtime.instances import InstanceManager

logger = logging.getLogger(__name__)


def build_tool_wrapper(
    tool: RegisteredTool,
    impl: Callable,
    instance_manager: InstanceManager | None = None,
) -> Callable:
    """Build a FastMCP-compatible wrapper for a callable.

    Args:
        tool: The registered tool.
        impl: The actual Python callable (function or unbound method).
        instance_manager: Manager for resolving class instances (required for methods).

    Returns:
        A new function suitable for FastMCP.tool().
    """
    is_method = tool.class_name is not None
    is_async = tool.is_async

    # If it's a simple function, we just need to handle FastMCP Context optionally
    if not is_method:
        return _build_function_wrapper(tool, impl, is_async)

    # If it's a method, we need the instance manager
    if not instance_manager:
        raise ValueError("instance_manager is required for wrapping methods")

    return _build_method_wrapper(tool, impl, instance_manager, is_async)


def _build_function_wrapper(
    tool: RegisteredTool,
    impl: Callable,
    is_async: bool,
) -> Callable:
    """Wrap a module-level function."""
    
    # Check if the original function expects a FastMCP Context
    sig = inspect.signature(impl)
    wants_ctx = any(
        p.annotation is Context or p.annotation == "Context"
        for p in sig.parameters.values()
    )

    if is_async:
        @functools.wraps(impl)
        async def _async_wrapper(ctx: Context = None, **kwargs: Any) -> Any:
            try:
                coerced_kwargs = coerce_arguments(tool, kwargs)
                if wants_ctx:
                    return await impl(ctx=ctx, **coerced_kwargs)
                return await impl(**coerced_kwargs)
            except Exception as e:
                logger.error("Tool execution failed: %s", e, exc_info=True)
                raise
        return _async_wrapper
    else:
        @functools.wraps(impl)
        def _sync_wrapper(ctx: Context = None, **kwargs: Any) -> Any:
            try:
                coerced_kwargs = coerce_arguments(tool, kwargs)
                if wants_ctx:
                    return impl(ctx=ctx, **coerced_kwargs)
                return impl(**coerced_kwargs)
            except Exception as e:
                logger.error("Tool execution failed: %s", e, exc_info=True)
                raise
        return _sync_wrapper


def _build_method_wrapper(
    tool: RegisteredTool,
    impl: Callable,
    manager: InstanceManager,
    is_async: bool,
) -> Callable:
    """Wrap a class method."""
    
    # Extract the class object from the unbound method
    if tool.extracted_obj:
        module_name, class_name = tool.extracted_obj.qualified_name.rsplit(".", 2)[:2]
    else:
        module_name = impl.__module__
        class_name = tool.class_name
        
    import importlib
    try:
        mod = importlib.import_module(module_name)
        cls_obj = getattr(mod, class_name) # type: ignore
    except Exception as e:
        raise RuntimeError(f"Could not resolve class {class_name} in module {module_name}: {e}")

    sig = inspect.signature(impl)
    wants_ctx = any(
        p.annotation is Context or p.annotation == "Context"
        for p in sig.parameters.values()
    )

    if is_async:
        @functools.wraps(impl)
        async def _async_method_wrapper(ctx: Context = None, **kwargs: Any) -> Any:
            try:
                # 1. Resolve instance
                instance = manager.get_instance(tool.class_name, cls_obj, ctx) # type: ignore
                
                # 2. Bind method
                coerced_kwargs = coerce_arguments(tool, kwargs)
                
                if wants_ctx:
                    return await impl(instance, ctx=ctx, **coerced_kwargs)
                return await impl(instance, **coerced_kwargs)
            except Exception as e:
                logger.error("Method execution failed: %s", e, exc_info=True)
                raise
        return _async_method_wrapper
    else:
        @functools.wraps(impl)
        def _sync_method_wrapper(ctx: Context = None, **kwargs: Any) -> Any:
            try:
                instance = manager.get_instance(tool.class_name, cls_obj, ctx) # type: ignore
                
                coerced_kwargs = coerce_arguments(tool, kwargs)
                
                if wants_ctx:
                    return impl(instance, ctx=ctx, **coerced_kwargs)
                return impl(instance, **coerced_kwargs)
            except Exception as e:
                logger.error("Method execution failed: %s", e, exc_info=True)
                raise
        return _sync_method_wrapper
