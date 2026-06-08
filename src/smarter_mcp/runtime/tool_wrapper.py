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

from smarter_mcp._registry import RegisteredTool
from smarter_mcp.errors import CoercionError, ToolExecutionError, format_error_response
from smarter_mcp.runtime.coercion import coerce_arguments
from smarter_mcp.runtime.instances import InstanceManager
from smarter_mcp.multimodal.interceptor import coerce_to_fastmcp_image

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

    if not is_method:
        wrapper = _build_function_wrapper(tool, impl, is_async)
    else:
        # If it's a method, we need the instance manager
        if not instance_manager:
            raise ValueError("instance_manager is required for wrapping methods")
        wrapper = _build_method_wrapper(tool, impl, instance_manager, is_async)

    # Rewrite signature to hide complex types from FastMCP/Pydantic
    sig = inspect.signature(impl)
    new_params = []
    first_param = True
    for name, param in sig.parameters.items():
        if is_method and first_param:
            first_param = False
            continue
        type_str = ""
        if param.annotation != inspect.Parameter.empty:
            type_str = getattr(param.annotation, "__name__", str(param.annotation)).lower()

        if "pil.image" in type_str or "image.image" in type_str or "ndarray" in type_str or "numpy.ndarray" in type_str or type_str in ("image", "pil_image"):
            new_params.append(param.replace(annotation=str))
        else:
            new_params.append(param)
            
    wrapper.__signature__ = sig.replace(parameters=new_params)
    return wrapper


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
                    res = await impl(ctx=ctx, **coerced_kwargs)
                else:
                    res = await impl(**coerced_kwargs)
                return coerce_to_fastmcp_image(res)
            except CoercionError as e:
                logger.warning("Coercion error in tool '%s': %s", tool.name, e)
                return format_error_response(tool.name, e)
            except Exception as e:
                logger.error("Execution error in tool '%s': %s", tool.name, e, exc_info=True)
                return format_error_response(tool.name, ToolExecutionError(str(e)))
        return _async_wrapper
    else:
        @functools.wraps(impl)
        def _sync_wrapper(ctx: Context = None, **kwargs: Any) -> Any:
            try:
                coerced_kwargs = coerce_arguments(tool, kwargs)
                if wants_ctx:
                    res = impl(ctx=ctx, **coerced_kwargs)
                else:
                    res = impl(**coerced_kwargs)
                return coerce_to_fastmcp_image(res)
            except CoercionError as e:
                logger.warning("Coercion error in tool '%s': %s", tool.name, e)
                return format_error_response(tool.name, e)
            except Exception as e:
                logger.error("Execution error in tool '%s': %s", tool.name, e, exc_info=True)
                return format_error_response(tool.name, ToolExecutionError(str(e)))
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
                instance = manager.get_instance(tool.class_name, cls_obj, ctx)  # type: ignore
                coerced_kwargs = coerce_arguments(tool, kwargs)
                if wants_ctx:
                    res = await impl(instance, ctx=ctx, **coerced_kwargs)
                else:
                    res = await impl(instance, **coerced_kwargs)
                return coerce_to_fastmcp_image(res)
            except CoercionError as e:
                logger.warning("Coercion error in tool '%s': %s", tool.name, e)
                return format_error_response(tool.name, e)
            except Exception as e:
                logger.error("Execution error in tool '%s': %s", tool.name, e, exc_info=True)
                return format_error_response(tool.name, ToolExecutionError(str(e)))
        return _async_method_wrapper
    else:
        @functools.wraps(impl)
        def _sync_method_wrapper(ctx: Context = None, **kwargs: Any) -> Any:
            try:
                instance = manager.get_instance(tool.class_name, cls_obj, ctx)  # type: ignore
                coerced_kwargs = coerce_arguments(tool, kwargs)
                if wants_ctx:
                    res = impl(instance, ctx=ctx, **coerced_kwargs)
                else:
                    res = impl(instance, **coerced_kwargs)
                return coerce_to_fastmcp_image(res)
            except CoercionError as e:
                logger.warning("Coercion error in tool '%s': %s", tool.name, e)
                return format_error_response(tool.name, e)
            except Exception as e:
                logger.error("Execution error in tool '%s': %s", tool.name, e, exc_info=True)
                return format_error_response(tool.name, ToolExecutionError(str(e)))
        return _sync_method_wrapper
