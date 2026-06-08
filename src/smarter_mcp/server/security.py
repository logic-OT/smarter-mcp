"""
Security: API-key auth (custom header + native Bearer) and rate limiting.

Centralizes all security construction so build(), http_app(), and run()
share one source of truth for keys and middleware.

Two auth layers, both fed from the same key set:
1. Custom `X-API-Key` ASGI middleware — protects all HTTP routes (exempts /health).
2. FastMCP-native Bearer `StaticTokenVerifier` — wired into the root FastMCP for
   MCP-protocol clients that send `Authorization: Bearer <key>`.

Rate limiting uses our own SlidingWindowMiddleware (not FastMCP's) because
FastMCP's implementation never evicts dead session buckets, causing unbounded
memory growth on long-running servers with many short-lived connections.
Our version evicts stale buckets inline — no background thread needed.
"""

from __future__ import annotations

import os
import time
import logging
from collections import deque
from typing import TYPE_CHECKING, Any

import anyio
from fastmcp.server.middleware.middleware import Middleware, MiddlewareContext
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

if TYPE_CHECKING:
    from smarter_mcp.config.manifest import ServerConfig

logger = logging.getLogger(__name__)

DEFAULT_EXEMPT_PATHS = frozenset({"/health"})


def load_api_keys(env_var: str) -> set[str]:
    """Read comma-separated API keys from the named environment variable.

    Returns an empty set if the variable is unset or blank.
    """
    raw = os.environ.get(env_var, "")
    return {k.strip() for k in raw.split(",") if k.strip()}


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests lacking a valid API key in the configured header.

    Exempt paths (e.g. /health) pass through unauthenticated so monitoring works.
    """

    def __init__(
        self,
        app: Any,
        header_name: str,
        valid_keys: set[str],
        exempt_paths: frozenset[str] = DEFAULT_EXEMPT_PATHS,
    ):
        super().__init__(app)
        self.header_name = header_name
        self.valid_keys = valid_keys
        self.exempt_paths = exempt_paths

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in self.exempt_paths:
            return await call_next(request)

        provided = request.headers.get(self.header_name)
        if not provided or provided not in self.valid_keys:
            return JSONResponse(
                {"error": "unauthorized", "message": f"Missing or invalid {self.header_name}"},
                status_code=401,
            )

        return await call_next(request)


def build_auth_provider(config: "ServerConfig"):
    """Build a FastMCP Bearer token verifier from the configured API keys.

    Returns None if auth is disabled or no keys are present.
    """
    if not config.auth_enabled:
        return None

    keys = load_api_keys(config.auth_keys_env)
    if not keys:
        logger.warning(
            "auth_enabled is True but no keys found in env var '%s'", config.auth_keys_env
        )
        return None

    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    tokens = {key: {"client_id": key, "scopes": []} for key in keys}
    return StaticTokenVerifier(tokens=tokens)


def session_client_id(context) -> str:
    """Best-effort per-session identifier for rate limiting.

    Falls back to "global" when no session id is available (e.g. stdio).
    """
    ctx = getattr(context, "fastmcp_context", None)
    if ctx is not None:
        session_id = getattr(ctx, "session_id", None)
        if session_id:
            return str(session_id)
    return "global"


class _SlidingWindow:
    """Per-client sliding-window counter. Tracks request timestamps in a deque."""

    __slots__ = ("timestamps", "last_seen")

    def __init__(self) -> None:
        self.timestamps: deque[float] = deque()
        self.last_seen: float = time.monotonic()

    def is_allowed(self, max_requests: int, window_seconds: float) -> bool:
        now = time.monotonic()
        self.last_seen = now
        cutoff = now - window_seconds
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()
        if len(self.timestamps) < max_requests:
            self.timestamps.append(now)
            return True
        return False


class SlidingWindowMiddleware(Middleware):
    """Sliding-window rate limiter with automatic eviction of stale session buckets.

    FastMCP's built-in SlidingWindowRateLimitingMiddleware stores one deque per
    session ID and never removes them — on a server with many short-lived
    connections the dict grows without bound. This implementation evicts any
    bucket whose last request is older than `evict_after_seconds` (default: 2×
    the rate window) during every request, so memory stays proportional to the
    number of *active* sessions, not all-time sessions.
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        get_client_id=None,
        evict_after_seconds: float | None = None,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.get_client_id = get_client_id
        self.evict_after = evict_after_seconds if evict_after_seconds is not None else window_seconds * 2
        self._buckets: dict[str, _SlidingWindow] = {}
        self._lock = anyio.Lock()

    def _client_id(self, context: MiddlewareContext) -> str:
        if self.get_client_id is not None:
            return self.get_client_id(context)
        return "global"

    def _evict_stale(self, now: float) -> None:
        cutoff = now - self.evict_after
        stale = [k for k, v in self._buckets.items() if v.last_seen < cutoff]
        for k in stale:
            del self._buckets[k]

    async def on_request(self, context: MiddlewareContext, call_next) -> Any:
        from mcp import McpError
        from mcp.types import ErrorData

        client_id = self._client_id(context)
        async with self._lock:
            self._evict_stale(time.monotonic())
            bucket = self._buckets.get(client_id)
            if bucket is None:
                bucket = _SlidingWindow()
                self._buckets[client_id] = bucket
            allowed = bucket.is_allowed(self.max_requests, self.window_seconds)

        if not allowed:
            raise McpError(ErrorData(
                code=-32000,
                message=f"Rate limit exceeded: {self.max_requests} requests per "
                        f"{int(self.window_seconds)}s for client {client_id}",
            ))
        return await call_next(context)


def build_rate_limit_middleware(config: "ServerConfig") -> list:
    """Build per-session and global sliding-window rate-limit middleware.

    Returns an empty list if rate limiting is disabled.
    """
    if not config.rate_limit_enabled:
        return []

    per_session = SlidingWindowMiddleware(
        max_requests=config.rate_limit_per_minute,
        window_seconds=60,
        get_client_id=session_client_id,
    )
    global_limit = SlidingWindowMiddleware(
        max_requests=config.rate_limit_global_per_minute,
        window_seconds=60,
    )
    return [per_session, global_limit]
