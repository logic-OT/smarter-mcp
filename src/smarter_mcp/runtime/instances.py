"""
Instance Lifecycle Manager.

Manages the instantiation of classes whose methods are exposed as tools.
Supports three lifecycles:
1. Session: One instance per FastMCP Context (connection).
2. Singleton: One instance shared globally.
3. Per-call: New instance created for every tool invocation.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from fastmcp import Context

from smarter_mcp.config.manifest import InstanceConfig

logger = logging.getLogger(__name__)


class InstanceManager:
    """Manages class instances for tool execution."""

    def __init__(self, configs: list[InstanceConfig]):
        """
        Args:
            configs: Instance configurations from the manifest.
        """
        self._configs = {c.class_name: c for c in configs}
        self._singletons: dict[str, Any] = {}
        # session_id → {class_name → instance}; keyed by the MCP session ID so
        # the same instance is reused across all tool calls within one connection.
        self._session_instances: dict[str, dict[str, Any]] = {}

    def add_config(self, class_name: str, lifecycle: str = "session", args: dict[str, Any] | None = None) -> None:
        """Programmatically add an instance configuration."""
        self._configs[class_name] = InstanceConfig(
            class_name=class_name,
            lifecycle=lifecycle, # type: ignore
            constructor_args=args or {}
        )

    def get_instance(
        self,
        class_name: str,
        cls_obj: type,
        ctx: Context | None = None,
    ) -> Any:
        """Get or create an instance of the class.

        Args:
            class_name: Fully qualified class name.
            cls_obj: The actual Python class type.
            ctx: FastMCP context (required for session-scoped instances).

        Returns:
            An instance of the class.
        """
        config = self._configs.get(class_name)
        lifecycle = config.lifecycle if config else "session"

        if lifecycle == "singleton":
            return self._get_singleton(class_name, cls_obj, config)
        elif lifecycle == "session":
            return self._get_session_instance(class_name, cls_obj, config, ctx)
        elif lifecycle == "per-call":
            return self._create_instance(class_name, cls_obj, config)
        else:
            raise ValueError(f"Unknown lifecycle: {lifecycle}")

    def _get_singleton(
        self,
        class_name: str,
        cls_obj: type,
        config: InstanceConfig | None,
    ) -> Any:
        """Get or create a singleton instance."""
        if class_name not in self._singletons:
            self._singletons[class_name] = self._create_instance(class_name, cls_obj, config)
        return self._singletons[class_name]

    def _get_session_instance(
        self,
        class_name: str,
        cls_obj: type,
        config: InstanceConfig | None,
        ctx: Context | None,
    ) -> Any:
        """Get or create a session-scoped instance.

        Keyed by the MCP session ID, which is stable for the lifetime of a
        single client connection. Falls back to per-call when no context is
        available (e.g., direct invocation outside MCP).
        """
        if ctx is None:
            logger.warning(
                "No context provided for session-scoped class %s, falling back to per-call.",
                class_name,
            )
            return self._create_instance(class_name, cls_obj, config)

        # Prefer the stable session/client ID; fall back to object identity so
        # we always get a key even if FastMCP changes the Context API.
        session_id = str(
            getattr(ctx, "session_id", None)
            or getattr(ctx, "client_id", None)
            or id(ctx)
        )
        session_store = self._session_instances.setdefault(session_id, {})
        if class_name not in session_store:
            session_store[class_name] = self._create_instance(class_name, cls_obj, config)

        return session_store[class_name]

    def _create_instance(
        self,
        class_name: str,
        cls_obj: type,
        config: InstanceConfig | None,
    ) -> Any:
        """Create a new instance of the class."""
        logger.debug("Creating new instance of %s", class_name)

        if not config:
            # Default: try to call constructor with no args
            try:
                return cls_obj()
            except TypeError as e:
                raise RuntimeError(
                    f"Failed to instantiate {class_name} with no arguments. "
                    f"Please provide an InstanceConfig in the manifest. Error: {e}"
                ) from e

        if config.factory:
            # Call factory function
            # Note: The factory needs to be resolved from the module path
            # This is a bit tricky, we assume the factory is in the same module
            # or is fully qualified. For now, we resolve it relative to the class module.
            module = inspect.getmodule(cls_obj)
            factory_name = config.factory
            
            if "." in factory_name:
                # Fully qualified factory (simplified resolution)
                import importlib
                mod_name, func_name = factory_name.rsplit(".", 1)
                mod = importlib.import_module(mod_name)
                factory_func = getattr(mod, func_name)
            elif module:
                # Local to the class module
                factory_func = getattr(module, factory_name)
            else:
                raise RuntimeError(f"Could not resolve factory {factory_name}")

            return factory_func(**config.factory_args)
        
        # Call constructor with explicit args
        return cls_obj(**config.constructor_args)
