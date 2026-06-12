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

H7 security notes:
- API key comparisons use ``hmac.compare_digest`` against every configured key
  (no early-out) to prevent timing-oracle attacks.
- If ``auth_enabled=True`` but no keys are loaded, startup FAILS LOUDLY rather
  than silently allowing all traffic.

M18 security note:
- An IP-based HTTP rate limiter supplements the per-session MCP-layer limiter.
  The IP is read from the ASGI ``scope["client"]`` (socket address) — not from
  X-Forwarded-For, which is client-controlled and trivially spoofable.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
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


def _constant_time_key_check(provided: str, valid_keys: set[str]) -> bool:
    """Compare ``provided`` against every key in ``valid_keys`` using
    ``hmac.compare_digest`` on SHA-256 digests.

    I2: Both operands are hashed to a fixed-width SHA-256 digest before
    comparison.  ``hmac.compare_digest`` returns early when its two arguments
    have different lengths; hashing eliminates that length-oracle leak so
    comparison time is always constant regardless of how long (or short) the
    provided value is.

    All comparisons are executed regardless of intermediate results (no
    early-out) to prevent timing-oracle attacks that could enumerate valid keys
    character-by-character.

    M1 note: the ``if not provided`` early-exit in ``APIKeyMiddleware.dispatch``
    runs BEFORE this function is called, so an empty string is rejected without
    entering the constant-time path.  That short-circuit is safe: an attacker
    who knows the empty-string response is fast gains only the trivial fact that
    "" is not a valid key, which is always true — it does not narrow the search
    space for real keys.

    Returns True iff ``provided`` exactly matches at least one key.
    """
    provided_digest = hashlib.sha256(provided.encode()).digest()
    matched = False
    for key in valid_keys:
        key_digest = hashlib.sha256(key.encode()).digest()
        if hmac.compare_digest(provided_digest, key_digest):
            matched = True
        # Do NOT break — always iterate all keys (timing safety).
    return matched


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests lacking a valid API key in the configured header.

    Exempt paths (e.g. /health) pass through unauthenticated so monitoring
    works.

    H7: comparisons use ``hmac.compare_digest`` via ``_constant_time_key_check``
    so response latency does not leak key prefix information.
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

        provided = request.headers.get(self.header_name, "")
        if not provided or not _constant_time_key_check(provided, self.valid_keys):
            return JSONResponse(
                {
                    "error": "unauthorized",
                    "message": f"Missing or invalid {self.header_name}",
                },
                status_code=401,
            )

        return await call_next(request)


def assert_auth_keys_present(config: "ServerConfig") -> None:
    """Raise ``RuntimeError`` if auth is enabled but no keys are configured.

    H7 / A2: fail-closed guarantee — a server that is supposed to be
    authenticated must not start in a fail-open state because an operator
    forgot to set the key env var.  Call this at EVERY server startup path
    (build(), http_app(), run()) so the guard fires regardless of which public
    entrypoint is used.
    """
    if not config.auth_enabled:
        return
    keys = load_api_keys(config.auth_keys_env)
    if not keys:
        raise RuntimeError(
            f"auth_enabled=True but no API keys found in env var "
            f"'{config.auth_keys_env}'.  Set {config.auth_keys_env} to a "
            f"comma-separated list of keys, or set auth_enabled=False."
        )


def build_auth_provider(config: "ServerConfig"):
    """Build a FastMCP Bearer token verifier from the configured API keys.

    Returns None if auth is disabled.

    H7: if auth is enabled but no keys are present, raises ``RuntimeError``
    (fail-closed) rather than returning None and silently allowing all traffic.
    """
    if not config.auth_enabled:
        return None

    keys = load_api_keys(config.auth_keys_env)
    if not keys:
        raise RuntimeError(
            f"auth_enabled=True but no API keys found in env var "
            f"'{config.auth_keys_env}'.  Set {config.auth_keys_env} to a "
            f"comma-separated list of keys, or set auth_enabled=False."
        )

    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    tokens = {key: {"client_id": key, "scopes": []} for key in keys}
    return StaticTokenVerifier(tokens=tokens)


def session_client_id(context) -> str:
    """Best-effort per-session identifier for rate limiting.

    Falls back to "global" when no session id is available (e.g. stdio).

    M18: per-session limits are advisory — a client can reset its window by
    reconnecting and getting a new session_id.  The IP-level HTTP middleware
    (``IPRateLimitMiddleware``) provides the non-bypassable backstop for HTTP
    transports.
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
        self.evict_after = (
            evict_after_seconds
            if evict_after_seconds is not None
            else window_seconds * 2
        )
        self._buckets: dict[str, _SlidingWindow] = {}
        # M3: lazy-init so the Lock is created inside an active event loop,
        # matching the pattern already used by IPRateLimitMiddleware.
        self._lock: anyio.Lock | None = None

    def _get_lock(self) -> anyio.Lock:
        if self._lock is None:
            self._lock = anyio.Lock()
        return self._lock

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
        async with self._get_lock():
            self._evict_stale(time.monotonic())
            bucket = self._buckets.get(client_id)
            if bucket is None:
                bucket = _SlidingWindow()
                self._buckets[client_id] = bucket
            allowed = bucket.is_allowed(self.max_requests, self.window_seconds)

        if not allowed:
            raise McpError(
                ErrorData(
                    code=-32000,
                    message=(
                        f"Rate limit exceeded: {self.max_requests} requests per "
                        f"{int(self.window_seconds)}s for client {client_id}"
                    ),
                )
            )
        return await call_next(context)


class IPRateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window HTTP rate limiter keyed on socket-derived client IP.

    M18: per-session MCP rate limits are advisory because a client can reconnect
    to get a fresh session_id and reset its window.  This middleware provides a
    non-bypassable backstop for HTTP transports by keying the limit on the
    TCP source address from ``scope["client"]`` — not X-Forwarded-For, which is
    trivially spoofable.

    Stale buckets are evicted inline (same approach as SlidingWindowMiddleware)
    to bound memory on long-running servers.
    """

    def __init__(
        self,
        app: Any,
        max_requests: int,
        window_seconds: float,
        evict_after_seconds: float | None = None,
        exempt_paths: frozenset[str] = DEFAULT_EXEMPT_PATHS,
    ) -> None:
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.evict_after = (
            evict_after_seconds
            if evict_after_seconds is not None
            else window_seconds * 2
        )
        self.exempt_paths = exempt_paths
        self._buckets: dict[str, _SlidingWindow] = {}
        self._lock: anyio.Lock | None = None  # lazy-init (anyio event-loop bound)

    def _get_lock(self) -> anyio.Lock:
        if self._lock is None:
            self._lock = anyio.Lock()
        return self._lock

    def _client_ip(self, request: Request) -> str:
        """Derive client IP from socket scope — NOT from HTTP headers."""
        client = request.scope.get("client")
        if client and isinstance(client, (tuple, list)) and len(client) >= 1:
            return str(client[0])
        # M5: scope["client"] is None for UNIX-socket or in-process transports
        # (e.g. Starlette TestClient with no client set).  All such requests
        # share the "unknown" bucket, so in-process callers collectively consume
        # from one window — acceptable since this path is not reachable from
        # an external network interface.
        return "unknown"

    def _evict_stale(self, now: float) -> None:
        cutoff = now - self.evict_after
        stale = [k for k, v in self._buckets.items() if v.last_seen < cutoff]
        for k in stale:
            del self._buckets[k]

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in self.exempt_paths:
            return await call_next(request)

        ip = self._client_ip(request)
        lock = self._get_lock()

        async with lock:
            self._evict_stale(time.monotonic())
            bucket = self._buckets.get(ip)
            if bucket is None:
                bucket = _SlidingWindow()
                self._buckets[ip] = bucket
            allowed = bucket.is_allowed(self.max_requests, self.window_seconds)

        if not allowed:
            return JSONResponse(
                {
                    "error": "rate_limit_exceeded",
                    "message": (
                        f"Rate limit exceeded: {self.max_requests} requests "
                        f"per {int(self.window_seconds)}s"
                    ),
                },
                status_code=429,
            )
        return await call_next(request)


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


def build_ip_rate_limit_middleware(
    config: "ServerConfig",
    *,
    app: Any,
) -> Any | None:
    """Build an IP-based HTTP rate-limiter (M18).

    Returns None when rate limiting is disabled, otherwise a configured
    ``IPRateLimitMiddleware`` wrapping ``app``.  This is an ASGI middleware
    so it must be applied to the Starlette application, not the MCP layer.
    """
    if not config.rate_limit_enabled:
        return None
    return IPRateLimitMiddleware(
        app,
        max_requests=config.rate_limit_per_minute,
        window_seconds=60,
    )
