"""
Wrappers that adapt unbound callables into FastMCP tools.

These wrappers handle:
1. FastMCP Context injection (via get_context() rather than injected params).
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
from smarter_mcp.extractor.models import CallableKind
from smarter_mcp.runtime.coercion import coerce_arguments
from smarter_mcp.runtime.instances import InstanceManager
from smarter_mcp.multimodal.interceptor import coerce_to_fastmcp_image

logger = logging.getLogger(__name__)


def _resolve_context() -> Context | None:
    """Retrieve the active FastMCP Context for the current request.

    Uses FastMCP's ContextVar-backed get_context() so we always get the real
    session context without relying on FastMCP injecting it via the wrapper's
    signature (which is forged and does not expose a ctx parameter to the
    MCP schema).  Returns None when called outside an active MCP request
    (e.g. direct unit-test invocation of the raw function).
    """
    try:
        from fastmcp.server.dependencies import get_context
        return get_context()
    except (RuntimeError, ImportError, AttributeError):
        return None


def build_tool_wrapper(
    tool: RegisteredTool,
    impl: Callable,
    instance_manager: InstanceManager | None = None,
    *,
    auto_detect: bool = True,
) -> Callable:
    """Build a FastMCP-compatible wrapper for a callable.

    Args:
        tool: The registered tool.
        impl: The actual Python callable (function or unbound method).
        instance_manager: Manager for resolving class instances (required for
            regular instance methods).

    Returns:
        A new function suitable for FastMCP.tool().
    """
    kind = tool.extracted_obj.kind if tool.extracted_obj else None
    is_method = tool.class_name is not None
    is_async = tool.is_async

    if not is_method or kind == CallableKind.STATICMETHOD:
        # Free functions and static methods: no instance injection.
        wrapper = _build_function_wrapper(tool, impl, is_async, auto_detect=auto_detect)
    elif kind == CallableKind.CLASSMETHOD:
        # Classmethods: the impl retrieved via getattr(cls, name) is already
        # bound to the class (Python's descriptor protocol provides cls
        # automatically).  Treat like a plain function — no instance injection.
        wrapper = _build_function_wrapper(tool, impl, is_async, auto_detect=auto_detect)
    else:
        # Regular instance method: needs an instance from the manager.
        if not instance_manager:
            raise ValueError("instance_manager is required for wrapping methods")
        wrapper = _build_method_wrapper(
            tool, impl, instance_manager, is_async, auto_detect=auto_detect
        )

    # Forge the FastMCP-visible signature from the impl's parameters.
    #
    # Rules for which first param to drop:
    #   - Regular METHOD:   drop 'self' (first param of the unbound function).
    #   - CLASSMETHOD:      keep all params; Python's descriptor already hides
    #                       'cls', so inspect.signature shows only real params.
    #   - STATICMETHOD:     keep all params; no implicit first param at all.
    #   - Free function:    keep all params.
    #
    # The wrapper obtains its Context via _resolve_context() at call time, so
    # 'ctx' / 'context' parameters that belong to the *impl* are deliberately
    # excluded from the schema (FastMCP must not prompt users for a Context
    # value).  We strip them here to keep them out of the JSON schema.
    sig = inspect.signature(impl)
    should_skip_first = (
        is_method
        and kind not in (CallableKind.STATICMETHOD, CallableKind.CLASSMETHOD)
    )
    new_params = []
    first_param = True
    for name, param in sig.parameters.items():
        if should_skip_first and first_param:
            first_param = False
            continue  # drop 'self'
        first_param = False

        # Skip Context-annotated params: the wrapper injects them via
        # _resolve_context(); they must not appear in the MCP tool schema.
        # Also strip Context | None and Optional[Context] (same injection).
        _ann = param.annotation
        _is_ctx = (
            _ann is Context
            or _ann == "Context"
            or (isinstance(_ann, str) and "Context" in _ann)
            or (
                getattr(_ann, "__args__", None) is not None
                and any(a is Context for a in _ann.__args__)  # type: ignore[union-attr]
            )
        )
        if _is_ctx:
            continue

        type_str = ""
        if param.annotation != inspect.Parameter.empty:
            type_str = getattr(
                param.annotation, "__name__", str(param.annotation)
            ).lower()

        if (
            "pil.image" in type_str
            or "image.image" in type_str
            or "ndarray" in type_str
            or "numpy.ndarray" in type_str
            or type_str in ("image", "pil_image")
        ):
            new_params.append(param.replace(annotation=str))
        else:
            new_params.append(param)

    wrapper.__signature__ = sig.replace(parameters=new_params)

    # Forge __annotations__ to match the forged signature.
    #
    # Pydantic resolves annotation strings via the function's *module* globals
    # (obtained via wrapper.__module__ + sys.modules lookup), NOT via the
    # function's __globals__ dict.  functools.wraps copies __module__ from
    # impl, so Pydantic looks in impl's module namespace — which may not
    # have Context in scope (e.g. if it was imported only locally inside a
    # test or tool function under `from __future__ import annotations`).
    #
    # By setting __annotations__ to exactly the params in the forged
    # signature, we ensure Pydantic never tries to resolve stripped params
    # such as 'context: Context'.  Regular params keep their annotations so
    # the generated JSON schema is still accurate.
    new_annotations: dict[str, Any] = {}
    for p in new_params:
        if p.annotation != inspect.Parameter.empty:
            new_annotations[p.name] = p.annotation
    # Preserve the return annotation so FastMCP can infer output schema.
    if sig.return_annotation != inspect.Parameter.empty:
        new_annotations["return"] = sig.return_annotation
    wrapper.__annotations__ = new_annotations

    return wrapper


def _detect_context_param(sig: inspect.Signature) -> str | None:
    """Return the name of the first Context-annotated parameter, or None.

    Matches bare ``Context``, union/optional types containing ``Context``
    (e.g. ``Context | None``, ``Optional[Context]``), and PEP-563 string
    annotations like ``"Context | None"`` or ``"Optional[Context]"``.
    """
    for pname, p in sig.parameters.items():
        ann = p.annotation
        if ann is inspect.Parameter.empty:
            continue
        if ann is Context or ann == "Context":
            return pname
        # String annotation (PEP-563 / from __future__ import annotations).
        if isinstance(ann, str) and "Context" in ann:
            return pname
        # Runtime union/optional: typing.Union[Context, ...] or Context | None.
        args = getattr(ann, "__args__", None)
        if args and any(a is Context for a in args):
            return pname
    return None


def _build_function_wrapper(
    tool: RegisteredTool,
    impl: Callable,
    is_async: bool,
    *,
    auto_detect: bool = True,
) -> Callable:
    """Wrap a module-level function, static method, or (bound) class method."""

    sig = inspect.signature(impl)
    # M5 fix: use the actual name of the Context-annotated param, not a hard-
    # coded 'ctx'.  A tool with `context: Context` would previously receive a
    # TypeError because the wrapper called impl(ctx=…) instead of impl(context=…).
    ctx_param_name: str | None = _detect_context_param(sig)

    if is_async:
        @functools.wraps(impl)
        async def _async_wrapper(**kwargs: Any) -> Any:
            ctx = _resolve_context()
            try:
                coerced_kwargs = coerce_arguments(tool, kwargs)
                if ctx_param_name:
                    res = await impl(**{ctx_param_name: ctx}, **coerced_kwargs)
                else:
                    res = await impl(**coerced_kwargs)
                return coerce_to_fastmcp_image(res) if auto_detect else res
            except CoercionError as e:
                logger.warning("Coercion error in tool '%s': %s", tool.name, e)
                return format_error_response(tool.name, e)
            except Exception as e:
                logger.error(
                    "Execution error in tool '%s': %s", tool.name, e, exc_info=True
                )
                return format_error_response(tool.name, ToolExecutionError(str(e)))
        return _async_wrapper
    else:
        @functools.wraps(impl)
        def _sync_wrapper(**kwargs: Any) -> Any:
            ctx = _resolve_context()
            try:
                coerced_kwargs = coerce_arguments(tool, kwargs)
                if ctx_param_name:
                    res = impl(**{ctx_param_name: ctx}, **coerced_kwargs)
                else:
                    res = impl(**coerced_kwargs)
                return coerce_to_fastmcp_image(res) if auto_detect else res
            except CoercionError as e:
                logger.warning("Coercion error in tool '%s': %s", tool.name, e)
                return format_error_response(tool.name, e)
            except Exception as e:
                logger.error(
                    "Execution error in tool '%s': %s", tool.name, e, exc_info=True
                )
                return format_error_response(tool.name, ToolExecutionError(str(e)))
        return _sync_wrapper


def _build_method_wrapper(
    tool: RegisteredTool,
    impl: Callable,
    manager: InstanceManager,
    is_async: bool,
    *,
    auto_detect: bool = True,
) -> Callable:
    """Wrap a regular instance method."""

    # Resolve the class object so the instance manager can create instances.
    if tool.extracted_obj:
        module_name, class_name = tool.extracted_obj.qualified_name.rsplit(".", 2)[:2]
    else:
        module_name = impl.__module__
        class_name = tool.class_name

    import importlib
    try:
        mod = importlib.import_module(module_name)
        cls_obj = getattr(mod, class_name)  # type: ignore
    except Exception as e:
        raise RuntimeError(
            f"Could not resolve class {class_name} in module {module_name}: {e}"
        ) from e

    sig = inspect.signature(impl)
    # M5: detect actual Context param name in the impl.
    ctx_param_name: str | None = _detect_context_param(sig)

    if is_async:
        @functools.wraps(impl)
        async def _async_method_wrapper(**kwargs: Any) -> Any:
            # C2 fix: obtain context via get_context() so the session ID is
            # always available regardless of how FastMCP forged the signature.
            ctx = _resolve_context()
            try:
                instance = manager.get_instance(tool.class_name, cls_obj, ctx)  # type: ignore
                coerced_kwargs = coerce_arguments(tool, kwargs)
                if ctx_param_name:
                    res = await impl(instance, **{ctx_param_name: ctx}, **coerced_kwargs)
                else:
                    res = await impl(instance, **coerced_kwargs)
                return coerce_to_fastmcp_image(res) if auto_detect else res
            except CoercionError as e:
                logger.warning("Coercion error in tool '%s': %s", tool.name, e)
                return format_error_response(tool.name, e)
            except Exception as e:
                logger.error(
                    "Execution error in tool '%s': %s", tool.name, e, exc_info=True
                )
                return format_error_response(tool.name, ToolExecutionError(str(e)))
        return _async_method_wrapper
    else:
        @functools.wraps(impl)
        def _sync_method_wrapper(**kwargs: Any) -> Any:
            ctx = _resolve_context()
            try:
                instance = manager.get_instance(tool.class_name, cls_obj, ctx)  # type: ignore
                coerced_kwargs = coerce_arguments(tool, kwargs)
                if ctx_param_name:
                    res = impl(instance, **{ctx_param_name: ctx}, **coerced_kwargs)
                else:
                    res = impl(instance, **coerced_kwargs)
                return coerce_to_fastmcp_image(res) if auto_detect else res
            except CoercionError as e:
                logger.warning("Coercion error in tool '%s': %s", tool.name, e)
                return format_error_response(tool.name, e)
            except Exception as e:
                logger.error(
                    "Execution error in tool '%s': %s", tool.name, e, exc_info=True
                )
                return format_error_response(tool.name, ToolExecutionError(str(e)))
        return _sync_method_wrapper
