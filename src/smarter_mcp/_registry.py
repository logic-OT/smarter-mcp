from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .extractor.models import ExtractedCallable, ExtractedModule, ExtractionResult

logger = logging.getLogger(__name__)


class _HasModules(Protocol):
    """Structural type for any object that exposes a list of ExtractedModules.

    Both ``ExtractionResult`` and ``FilterResult`` satisfy this protocol,
    allowing ``merge_extraction`` to accept either without an explicit Union.
    """

    modules: list[ExtractedModule]


@dataclass
class RegisteredTool:
    """A tool registered with Smarter-MCP."""
    name: str
    description: str | None
    fn: Callable
    namespace: str
    source: Literal["decorator", "discovery"]
    class_name: str | None = None
    is_async: bool = False
    tests: list[dict[str, Any]] = field(default_factory=list)
    extracted_obj: ExtractedCallable | None = None


@dataclass
class RegisteredResource:
    """A resource registered with Smarter-MCP."""
    uri: str
    description: str | None
    fn: Callable
    namespace: str
    source: Literal["decorator", "discovery"]
    extracted_obj: ExtractedCallable | None = None


@dataclass
class RegisteredToolkit:
    """A toolkit class registered with Smarter-MCP."""
    cls: type
    class_name: str
    namespace: str
    lifecycle: Literal["session", "singleton", "per-call"]
    constructor_args: dict[str, Any]
    tools: list[RegisteredTool] = field(default_factory=list)


class ToolRegistry:
    """Single source of truth for all tools, resources, and toolkits."""

    def __init__(self):
        # namespace -> name -> tool
        self._tools: dict[str, dict[str, RegisteredTool]] = {}
        # namespace -> uri -> resource
        self._resources: dict[str, dict[str, RegisteredResource]] = {}
        # class_name -> toolkit
        self._toolkits: dict[str, RegisteredToolkit] = {}

    def register_tool(
        self,
        fn: Callable,
        *,
        name: str | None = None,
        description: str | None = None,
        namespace: str = "default",
        class_name: str | None = None,
        tests: list[dict[str, Any]] | None = None,
        source: Literal["decorator", "discovery"] = "decorator"
    ) -> RegisteredTool:
        tool_name = name or fn.__name__
        is_async = inspect.iscoroutinefunction(fn)

        tool = RegisteredTool(
            name=tool_name,
            description=description,
            fn=fn,
            namespace=namespace,
            source=source,
            class_name=class_name,
            is_async=is_async,
            tests=tests or []
        )

        if namespace not in self._tools:
            self._tools[namespace] = {}

        self._tools[namespace][tool_name] = tool
        return tool

    def register_resource(
        self,
        fn: Callable,
        *,
        uri: str,
        description: str | None = None,
        namespace: str = "default",
        source: Literal["decorator", "discovery"] = "decorator"
    ) -> RegisteredResource:
        res = RegisteredResource(
            uri=uri,
            description=description,
            fn=fn,
            namespace=namespace,
            source=source
        )

        if namespace not in self._resources:
            self._resources[namespace] = {}

        self._resources[namespace][uri] = res
        return res

    def register_toolkit(
        self,
        cls: type,
        *,
        lifecycle: Literal["session", "singleton", "per-call"] = "session",
        namespace: str = "default",
        constructor_args: dict[str, Any] | None = None
    ) -> RegisteredToolkit:
        tk = RegisteredToolkit(
            cls=cls,
            class_name=cls.__name__,
            namespace=namespace,
            lifecycle=lifecycle,
            constructor_args=constructor_args or {}
        )
        _toolkit_key = f"{cls.__module__}.{cls.__qualname__}"
        existing = self._toolkits.get(_toolkit_key)
        if existing is not None and existing.cls is not cls:
            logger.warning(
                "Toolkit collision: '%s' is already registered. "
                "The new registration will overwrite it.",
                _toolkit_key,
            )
        self._toolkits[_toolkit_key] = tk
        return tk

    def merge_extraction(
        self,
        extraction: _HasModules,
        implementations: dict[str, Callable],
        namespace_override: str | None = None
    ) -> None:
        """Merge auto-discovered tools/resources into the registry."""
        for mod in extraction.modules:
            # H12: use the full dotted module path for the namespace so that
            # a/utils.py (module "a.utils") and b/utils.py (module "b.utils")
            # get different namespaces ("a_utils" vs "b_utils") and do not
            # silently collide on the last segment "utils".
            if namespace_override:
                ns = namespace_override
            elif mod.module_name:
                ns = "_".join(mod.module_name.split("."))
            else:
                ns = "default"

            for obj in mod.all_callables:
                # Resolve the implementation function
                impl_key = f"{mod.module_name}.{obj.simple_name}"
                if obj.class_name:
                    impl_key = f"{mod.module_name}.{obj.class_name}.{obj.simple_name}"

                fn = implementations.get(impl_key)
                if not fn:
                    continue

                is_resource = getattr(fn, "_smarter_mcp_resource", False)
                if obj.kind == "property" or is_resource:
                    uri = getattr(fn, "_smarter_mcp_uri", None) or f"resource://{ns}/{obj.class_name}/{obj.simple_name}"
                    description = getattr(fn, "_smarter_mcp_description", None) or obj.docstring

                    res = RegisteredResource(
                        uri=uri,
                        description=description,
                        fn=fn,
                        namespace=ns,
                        source="decorator" if is_resource else "discovery",
                        extracted_obj=obj
                    )
                    if ns not in self._resources:
                        self._resources[ns] = {}
                    self._resources[ns][uri] = res
                else:
                    is_tool_decorator = getattr(fn, "_smarter_mcp_tool", False)
                    tool_name = getattr(fn, "_smarter_mcp_name", None) or obj.tool_name
                    description = getattr(fn, "_smarter_mcp_description", None) or obj.docstring
                    tests = getattr(fn, "_smarter_mcp_tests", [])

                    tool = RegisteredTool(
                        name=tool_name,
                        description=description,
                        fn=fn,
                        namespace=ns,
                        source="decorator" if is_tool_decorator else "discovery",
                        class_name=obj.class_name,
                        is_async=obj.is_async,
                        tests=tests,
                        extracted_obj=obj
                    )
                    if ns not in self._tools:
                        self._tools[ns] = {}

                    existing = self._tools[ns].get(tool_name)
                    if existing is None:
                        self._tools[ns][tool_name] = tool
                    elif existing.source == "decorator":
                        # Decorator-registered tools always win silently.
                        pass
                    else:
                        # H12: warn on silent collision so operators know what
                        # happened instead of getting last-write-wins silently.
                        logger.warning(
                            "Tool name collision in namespace '%s': '%s' is being "
                            "overwritten (previous source=%s, new source=%s). "
                            "Consider namespace overrides or renaming.",
                            ns, tool_name, existing.source, tool.source,
                        )
                        self._tools[ns][tool_name] = tool

    def merge_module(
        self,
        extraction: ExtractionResult,
        module: Any,
        namespace_override: str | None = None
    ) -> None:
        """Merge an imported module directly into the registry."""
        impls = {}
        for mod in extraction.modules:
            for obj in mod.all_callables:
                impl_key = f"{mod.module_name}.{obj.simple_name}"
                if obj.class_name:
                    impl_key = f"{mod.module_name}.{obj.class_name}.{obj.simple_name}"

                try:
                    if obj.class_name:
                        cls_obj = getattr(module, obj.class_name)
                        impls[impl_key] = getattr(cls_obj, obj.simple_name)
                    else:
                        impls[impl_key] = getattr(module, obj.simple_name)
                except AttributeError:
                    continue

        self.merge_extraction(extraction, impls, namespace_override=namespace_override)

    def get_namespace_tools(self, namespace: str) -> list[RegisteredTool]:
        return list(self._tools.get(namespace, {}).values())

    def get_namespace_resources(self, namespace: str) -> list[RegisteredResource]:
        return list(self._resources.get(namespace, {}).values())

    def get_all_namespaces(self) -> set[str]:
        return set(self._tools.keys()) | set(self._resources.keys())

    def get_all_tools(self) -> list[RegisteredTool]:
        tools = []
        for ns_tools in self._tools.values():
            tools.extend(ns_tools.values())
        return tools

    def index_by_name(self) -> dict[str, RegisteredTool]:
        """Build a lookup mapping tool names → tool for O(1) override matching.

        Each tool is indexed under both its registered name and its extracted
        qualified name (when available), so a manifest override targeting either
        form resolves in one dict lookup instead of an O(N) scan.

        Note: if two tools share a simple name across namespaces, the later one
        wins for that key — qualified names disambiguate. Matches the previous
        first-hit-wins loop behavior closely enough for override resolution.
        """
        index: dict[str, RegisteredTool] = {}
        for tool in self.get_all_tools():
            index.setdefault(tool.name, tool)
            if tool.extracted_obj is not None:
                index.setdefault(tool.extracted_obj.qualified_name, tool)
        return index
