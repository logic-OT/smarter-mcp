"""
Namespace Router — maps extracted modules to FastMCP sub-servers.

Each source module gets its own FastMCP sub-server, mounted on the root
server with a namespace derived from the module path. This gives clean,
collision-free tool naming:

    src/utils.py       → /mcp/utils       → FastMCP("utils")
    src/db/client.py   → /mcp/db_client   → FastMCP("db_client")
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from smarter_mcp._registry import RegisteredResource, RegisteredTool, ToolRegistry
from smarter_mcp.config.manifest import ManifestConfig, ToolOverride

if TYPE_CHECKING:
    from smarter_mcp.runtime.instances import InstanceManager

logger = logging.getLogger(__name__)


def _module_to_namespace(module_name: str, overrides: dict[str, str]) -> str:
    """Convert a dotted module name to a namespace string.

    Args:
        module_name: Dotted module name like 'mylib.db.client'.
        overrides: Custom namespace mappings from manifest.

    Returns:
        Namespace string like 'db_client'.
    """
    # Check explicit overrides first
    if module_name in overrides:
        return overrides[module_name]

    # Also check path-style override (db/client → db_client)
    path_key = module_name.replace(".", "/")
    if path_key in overrides:
        return overrides[path_key]

    # Auto-derive: strip the root package, join with underscore
    parts = module_name.split(".")
    # If it's a single-level module, use it directly
    if len(parts) == 1:
        return parts[0]

    # Otherwise use underscore-joined path
    return "_".join(parts)


def _build_tool_name(
    tool: RegisteredTool,
    separator: str = "_",
) -> str:
    """Generate the MCP tool name for a callable.

    Functions: use the function name directly.
    Methods: ClassName{separator}method_name to avoid collisions.

    H12: if the user explicitly set the name via ``@tool(name="...")``, that
    name is used verbatim — the class prefix is NOT stacked on top.
    """
    # An explicit @tool(name=...) sets _smarter_mcp_name on the function.
    # Respect that choice: don't prepend the class name.
    if getattr(tool.fn, "_smarter_mcp_name", None) is not None:
        return tool.name
    if tool.class_name and not tool.name.startswith(tool.class_name):
        return f"{tool.class_name}{separator}{tool.name}"
    return tool.name


def _build_tool_description(tool: RegisteredTool | RegisteredResource) -> str:
    """Return the full description for an MCP tool or resource.

    Explicit ``@tool("...")`` strings and LLM-generated descriptions may span
    multiple lines — truncating them to the first line discards meaningful
    content and wastes LLM-generated text.  Only auto-generated placeholders
    are necessarily terse.
    """
    if tool.description:
        # Return the full description unchanged — do NOT truncate to the first
        # line.  Multi-line docstrings and LLM descriptions must survive intact.
        return tool.description.strip()

    # Auto-generate a minimal placeholder when no description is available.
    if isinstance(tool, RegisteredTool):
        if tool.class_name:
            return f"{tool.class_name}.{tool.name}()"
        return f"{tool.name}()"
    elif isinstance(tool, RegisteredResource):
        return f"Resource: {tool.uri}"

    return ""


def _make_bound_getter(
    fn: Any,
    cls_name: str,
    cls_obj: type,
    manager: Any,
    resource_uri: str,
    log: Any,
) -> Any:
    """Return a zero-parameter callable that resolves an instance and calls *fn*.

    Using a factory function (instead of default-value parameters) means the
    returned ``_bound_getter`` has **no** visible parameters.  FastMCP inspects
    the signature to decide whether to treat the callable as a static resource
    or a resource template; default-value params look like MCP parameters and
    trigger "URI template must contain at least one parameter".
    """
    def _bound_getter() -> Any:
        ctx = None
        try:
            from fastmcp.server.dependencies import get_context
            ctx = get_context()
        except (ImportError, LookupError, RuntimeError):
            log.debug("get_context() unavailable for resource %s", resource_uri)
        instance = manager.get_instance(cls_name, cls_obj, ctx)
        return fn(instance)

    return _bound_getter


class NamespaceRouter:
    """Routes extracted modules to FastMCP sub-servers.

    Creates one FastMCP instance per source module, registers
    extracted callables as tools, then mounts everything on a
    root server.
    """

    def __init__(
        self,
        config: ManifestConfig,
        instance_manager: InstanceManager | None = None,
    ):
        """
        Args:
            config: Manifest configuration.
            instance_manager: Manager for resolving class instances.
        """
        self.config = config
        self.routing = config.routing
        self.instance_manager = instance_manager
        self._root: FastMCP | None = None
        self._namespaces: dict[str, FastMCP] = {}
        # O(1) override lookup, keyed by ToolOverride.function (built once).
        self._override_index: dict[str, ToolOverride] = {
            override.function: override for override in config.tools
        }

    def build_server(
        self,
        registry: ToolRegistry,
        auth: Any | None = None,
    ) -> FastMCP:
        """Build the complete FastMCP server from the registry.

        Args:
            registry: The central tool registry.
            auth: Optional FastMCP auth provider (Bearer token verifier).

        Returns:
            Root FastMCP server with all namespaces mounted.
        """
        self._root = FastMCP(
            name=self.config.name,
            instructions=self.config.description or f"{self.config.name} MCP Server",
            auth=auth,
        )

        for ns_name in registry.get_all_namespaces():
            sub_server = self._build_namespace_server(ns_name, registry)
            if sub_server:
                self._namespaces[ns_name] = sub_server
                # H12: the "default" namespace (used for @tool/@resource
                # decorator-registered callables) must be mounted WITHOUT a
                # prefix so a @tool greet is simply "greet", not "default_greet".
                if ns_name == "default":
                    self._root.mount(sub_server)  # namespace=None → no prefix
                else:
                    self._root.mount(sub_server, namespace=ns_name)

        return self._root

    def _build_namespace_server(
        self,
        namespace: str,
        registry: ToolRegistry,
    ) -> FastMCP | None:
        """Build a FastMCP sub-server for a single namespace."""
        sub = FastMCP(
            name=namespace,
            instructions=f"Tools from {namespace}",
        )

        tool_count = 0

        # Register tools
        for tool in registry.get_namespace_tools(namespace):
            if self._register_tool(sub, tool, namespace):
                tool_count += 1

        # Register resources
        for resource in registry.get_namespace_resources(namespace):
            self._register_resource(sub, resource, namespace)

        if tool_count == 0 and not registry.get_namespace_resources(namespace):
            return None

        logger.info("Namespace '%s': registered %d tools", namespace, tool_count)
        return sub

    def _register_tool(
        self,
        server: FastMCP,
        tool: RegisteredTool,
        namespace: str,
    ) -> bool:
        """Register a single tool on the server."""
        tool_name = _build_tool_name(tool, self.routing.separator)
        description = _build_tool_description(tool)

        # Check for manifest tool overrides (O(1) via the prebuilt index).
        # Match by qualified name if extracted_obj is present, else by simple name.
        qual_name = tool.extracted_obj.qualified_name if tool.extracted_obj else tool.name
        override = self._override_index.get(qual_name) or self._override_index.get(tool.name)
        if override is not None:
            if not override.expose:
                return False
            if override.name:
                tool_name = override.name
            if override.description:
                description = override.description

        impl = tool.fn

        from smarter_mcp.runtime.tool_wrapper import build_tool_wrapper
        impl = build_tool_wrapper(tool, impl, self.instance_manager)

        # Register with FastMCP
        try:
            server.tool(
                name=tool_name,
                description=description,
            )(impl)
            logger.debug("Registered tool: %s/%s", namespace, tool_name)
            return True
        except Exception as e:
            logger.warning("Failed to register tool %s: %s", tool_name, e)
            return False

    def _register_resource(
        self,
        server: FastMCP,
        resource: RegisteredResource,
        namespace: str,
    ) -> None:
        """Register a property as an MCP resource.

        H13: property getters are unbound ``fget`` functions that require a
        ``self`` argument.  FastMCP rejects such callables (it sees an
        unexpected first positional param).  We wrap them so that ``self`` is
        resolved via the InstanceManager at call time, exactly as
        ``_build_method_wrapper`` does for tools.
        """
        description = _build_tool_description(resource)
        impl = resource.fn

        # H13: bind property getter when we have class context
        if (
            resource.extracted_obj is not None
            and resource.extracted_obj.class_name is not None
            and self.instance_manager is not None
        ):
            class_name = resource.extracted_obj.class_name
            # Derive the module from the qualified name: "mod.Cls.prop" -> "mod"
            qualified = resource.extracted_obj.qualified_name
            qparts = qualified.rsplit(".", 2)
            if len(qparts) == 3:
                module_name = qparts[0]
            else:
                module_name = (
                    resource.extracted_obj.module_path.replace("/", ".").removesuffix(".py")
                )

            try:
                mod = importlib.import_module(module_name)
                cls_obj = getattr(mod, class_name)
            except Exception as exc:
                logger.warning(
                    "Cannot bind property resource %s: could not load %s.%s: %s",
                    resource.uri, module_name, class_name, exc,
                )
            else:
                impl = _make_bound_getter(
                    impl, class_name, cls_obj, self.instance_manager, resource.uri, logger
                )

        try:
            server.resource(resource.uri, description=description)(impl)
            logger.debug("Registered resource: %s", resource.uri)
        except Exception as e:
            logger.warning("Failed to register resource %s: %s", resource.uri, e)



    @property
    def namespaces(self) -> list[str]:
        """List of registered namespace names."""
        return list(self._namespaces.keys())

    @property
    def root_server(self) -> FastMCP | None:
        """The root FastMCP server instance."""
        return self._root
