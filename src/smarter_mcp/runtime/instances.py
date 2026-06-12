"""
Instance Lifecycle Manager.

Manages the instantiation of classes whose methods are exposed as tools.
Supports three lifecycles:
1. Session: One instance per FastMCP Context (connection).
2. Singleton: One instance shared globally.
3. Per-call: New instance created for every tool invocation.

Thread-safety notes
-------------------
FastMCP 3.x dispatches synchronous tools in a thread-pool, so multiple
threads may request the first instance for the same class at the same time.
Both _get_singleton and _get_session_instance guard creation with
``_creation_lock`` (a threading.Lock) to prevent double-instantiation.

Session eviction
----------------
``_session_instances`` is bounded to ``_MAX_SESSION_ENTRIES`` entries via a
simple LRU strategy (OrderedDict move_to_end / popitem(last=False)).  When a
slot is evicted the outgoing instance is best-effort-closed
(close/aclose/__exit__) so resources are released even without a formal
session-disconnect hook (FastMCP 3.3.1 does not expose one).
"""

from __future__ import annotations

import inspect
import logging
import threading
from collections import OrderedDict
from typing import Any, Callable

from fastmcp import Context

from smarter_mcp.config.manifest import InstanceConfig

logger = logging.getLogger(__name__)

# Maximum number of concurrent sessions tracked.  When this is exceeded the
# oldest session's instances are evicted (best-effort closed).
_MAX_SESSION_ENTRIES = 256


def _best_effort_close(instance: Any) -> None:
    """Attempt to release resources held by *instance*.

    Tries (in order): ``close()``, ``__exit__``.
    A failure in one path does not prevent the others from running.
    """
    for attr in ("close", "__exit__"):
        fn = getattr(instance, attr, None)
        if fn is None:
            continue
        result: Any = None
        try:
            if attr == "__exit__":
                fn(None, None, None)
            else:
                result = fn()
            # If close() returned a coroutine we can't await it here (sync
            # context).  Warn rather than silently discard.
            if inspect.isawaitable(result if attr == "close" else None):
                logger.warning(
                    "Instance %r has an async close(); cannot await from sync "
                    "eviction path.  Prefer a sync close() or __exit__.",
                    instance,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Error closing instance %r: %s", instance, exc)


class InstanceManager:
    """Manages class instances for tool execution."""

    def __init__(
        self,
        configs: list[InstanceConfig],
        max_sessions: int = _MAX_SESSION_ENTRIES,
    ):
        """
        Args:
            configs: Instance configurations from the manifest.
            max_sessions: Maximum number of concurrent sessions tracked before
                LRU eviction kicks in.  Defaults to ``_MAX_SESSION_ENTRIES``
                (256).  Pass a smaller value in tests or memory-constrained
                deployments.
        """
        self._configs = {c.class_name: c for c in configs}
        self._max_sessions = max_sessions
        self._singletons: dict[str, Any] = {}
        # session_id → {class_name → instance}; bounded LRU so memory stays
        # proportional to active sessions rather than all-time sessions.
        self._session_instances: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # Single lock covering both dicts.  Held only during the check-then-set
        # window, not during the (potentially slow) constructor call.
        self._creation_lock = threading.Lock()

    def add_config(self, class_name: str, lifecycle: str = "session", args: dict[str, Any] | None = None) -> None:
        """Programmatically add an instance configuration."""
        self._configs[class_name] = InstanceConfig(
            class_name=class_name,
            lifecycle=lifecycle,  # type: ignore
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
        """Get or create a singleton instance (thread-safe)."""
        # Fast path — no lock needed once created.
        if class_name in self._singletons:
            return self._singletons[class_name]

        with self._creation_lock:
            # Re-check after acquiring the lock (another thread may have
            # created it between the check above and acquiring the lock).
            if class_name not in self._singletons:
                self._singletons[class_name] = self._create_instance(
                    class_name, cls_obj, config
                )
        return self._singletons[class_name]

    def _get_session_instance(
        self,
        class_name: str,
        cls_obj: type,
        config: InstanceConfig | None,
        ctx: Context | None,
    ) -> Any:
        """Get or create a session-scoped instance (thread-safe + LRU-bounded).

        Keyed by the MCP session ID, which is stable for the lifetime of a
        single client connection.  Falls back to per-call when no context is
        available (e.g. direct invocation outside MCP).
        """
        if ctx is None:
            logger.warning(
                "No context provided for session-scoped class %s, falling back to per-call.",
                class_name,
            )
            return self._create_instance(class_name, cls_obj, config)

        _sid_raw = getattr(ctx, "session_id", None) or getattr(ctx, "client_id", None)
        if _sid_raw is None:
            logger.warning(
                "Context for session-scoped class %s has no session_id or "
                "client_id; falling back to id(ctx)=%d.  Instance will not "
                "be reused across calls that receive a different ctx object.",
                class_name,
                id(ctx),
            )
            session_id = str(id(ctx))
        else:
            session_id = str(_sid_raw)

        # Fast path — check without the lock first.
        session_store = self._session_instances.get(session_id)
        if session_store is not None and class_name in session_store:
            # Promote to most-recently-used.  Guard against a concurrent
            # eviction removing session_id between the check above and
            # acquiring the lock (which would cause move_to_end to KeyError).
            with self._creation_lock:
                if session_id in self._session_instances:
                    self._session_instances.move_to_end(session_id)
            return session_store[class_name]

        evicted: list[Any] = []
        with self._creation_lock:
            # Re-check inside the lock.
            session_store = self._session_instances.get(session_id)
            if session_store is None:
                session_store = {}
                self._session_instances[session_id] = session_store
                evicted = self._evict_if_over_limit()
            elif class_name in session_store:
                self._session_instances.move_to_end(session_id)
                return session_store[class_name]

            session_store[class_name] = self._create_instance(
                class_name, cls_obj, config
            )
            self._session_instances.move_to_end(session_id)

        # Close evicted instances outside the lock so slow I/O does not
        # stall concurrent instance creation (module docstring promise).
        for inst in evicted:
            _best_effort_close(inst)

        return session_store[class_name]

    def _evict_if_over_limit(self) -> list[Any]:
        """Evict the oldest session entry when the dict exceeds the size cap.

        Must be called while holding ``_creation_lock``.
        Returns the evicted instances so the caller can close them outside
        the lock, keeping slow I/O off the critical section.
        """
        evicted: list[Any] = []
        while len(self._session_instances) > self._max_sessions:
            _sid, old_store = self._session_instances.popitem(last=False)
            logger.debug(
                "Session instance LRU eviction: session_id=%s, classes=%s",
                _sid,
                list(old_store.keys()),
            )
            evicted.extend(old_store.values())
        return evicted

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
            module = inspect.getmodule(cls_obj)
            factory_name = config.factory

            if "." in factory_name:
                import importlib
                mod_name, func_name = factory_name.rsplit(".", 1)
                mod = importlib.import_module(mod_name)
                factory_func = getattr(mod, func_name)
            elif module:
                factory_func = getattr(module, factory_name)
            else:
                raise RuntimeError(f"Could not resolve factory {factory_name}")

            return factory_func(**config.factory_args)

        # Call constructor with explicit args
        return cls_obj(**config.constructor_args)
