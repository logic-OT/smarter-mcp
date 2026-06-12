"""Tests for M18 — per-IP rate limiting.

- IPRateLimitMiddleware triggers on repeated requests from the same IP.
- IP is derived from scope["client"] (socket), not X-Forwarded-For.
- Per-session MCP limits are documented as advisory (tested separately).
"""

from __future__ import annotations

from smarter_mcp.server.security import IPRateLimitMiddleware


class TestIPRateLimitMiddleware:
    def _make_app(self, max_requests: int, window_seconds: float = 60.0):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        def ok(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/test", ok)])
        app.add_middleware(
            IPRateLimitMiddleware,
            max_requests=max_requests,
            window_seconds=window_seconds,
            exempt_paths=frozenset(),
        )
        return app

    def test_requests_under_limit_pass(self):
        from starlette.testclient import TestClient

        app = self._make_app(max_requests=5)
        client = TestClient(app, raise_server_exceptions=False)

        for i in range(5):
            resp = client.get("/test")
            assert resp.status_code == 200, f"Request {i+1} should pass"

    def test_requests_over_limit_rejected(self):
        from starlette.testclient import TestClient

        app = self._make_app(max_requests=3)
        client = TestClient(app, raise_server_exceptions=False)

        for i in range(3):
            resp = client.get("/test")
            assert resp.status_code == 200

        # 4th request must be rejected
        resp = client.get("/test")
        assert resp.status_code == 429, (
            f"Expected 429 after exceeding IP rate limit, got {resp.status_code}"
        )

    def test_ip_derived_from_scope_not_headers(self):
        """Rate limit must key on socket IP, not X-Forwarded-For header."""
        from starlette.testclient import TestClient

        app = self._make_app(max_requests=2)
        client = TestClient(app, raise_server_exceptions=False)

        # Hit the limit from the real socket IP
        for _ in range(2):
            client.get("/test")

        # Spoofing X-Forwarded-For with a different IP must NOT reset the limit
        resp = client.get(
            "/test", headers={"X-Forwarded-For": "1.2.3.4"}
        )
        assert resp.status_code == 429, (
            "X-Forwarded-For spoofing must not bypass the IP rate limit"
        )

    def test_exempt_path_bypasses_limit(self):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        def ok(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/health", ok), Route("/test", ok)])
        app.add_middleware(
            IPRateLimitMiddleware,
            max_requests=2,
            window_seconds=60.0,
            exempt_paths=frozenset({"/health"}),
        )
        client = TestClient(app, raise_server_exceptions=False)

        # Hit the rate limit on /test
        client.get("/test")
        client.get("/test")
        assert client.get("/test").status_code == 429

        # /health must be exempt
        for _ in range(10):
            resp = client.get("/health")
            assert resp.status_code == 200, "Exempt path must bypass IP rate limit"


class TestC3IPRateLimitWiredIntoASGI:
    """C3: IPRateLimitMiddleware must be wired into the ASGI stack (not dead code).

    Previously, build_ip_rate_limit_middleware existed and was unit-tested but
    _asgi_middleware() never added it to the stack, so it never ran on real
    http_app() / run() calls.  This end-to-end test verifies that after building
    a SmarterMCP with rate_limit_enabled=True, the http_app() ASGI stack
    enforces IP-level 429s.
    """

    def test_ip_rate_limit_enforced_through_http_app(self):
        """SmarterMCP.http_app() with rate_limit_enabled must return 429 after the limit."""
        from starlette.testclient import TestClient

        from smarter_mcp import SmarterMCP
        from smarter_mcp._decorators import clear_global_registry

        clear_global_registry()
        try:
            app = SmarterMCP(
                "c3-rate-limit-e2e",
                rate_limit_enabled=True,
                rate_limit_per_minute=2,  # very low limit for testing
            )
            asgi_app = app.http_app()
            client = TestClient(asgi_app, raise_server_exceptions=False)

            # /mcp/default/schema is not in the default exempt paths, so it is
            # rate-limited.  It may return 404 (no tools) but must NOT be 429
            # for the first N requests within the limit.
            for i in range(2):
                resp = client.get("/mcp/default/schema")
                assert resp.status_code != 429, (
                    f"Request {i + 1} should not be rate-limited yet, "
                    f"got {resp.status_code}"
                )

            # The next request must be rate-limited.
            resp = client.get("/mcp/default/schema")
            assert resp.status_code == 429, (
                f"Expected 429 after exceeding IP rate limit, got {resp.status_code}. "
                "IPRateLimitMiddleware may not be wired into the ASGI stack (C3)."
            )
        finally:
            clear_global_registry()

    def test_ip_rate_limit_not_applied_when_disabled(self):
        """With rate_limit_enabled=False, no 429 is returned regardless of request count."""
        from starlette.testclient import TestClient

        from smarter_mcp import SmarterMCP
        from smarter_mcp._decorators import clear_global_registry

        clear_global_registry()
        try:
            app = SmarterMCP("c3-rate-limit-off", rate_limit_enabled=False)
            asgi_app = app.http_app()
            client = TestClient(asgi_app, raise_server_exceptions=False)

            for _ in range(10):
                resp = client.get("/mcp/default/schema")
                assert resp.status_code != 429, (
                    "Rate limiting is disabled — no request should return 429"
                )
        finally:
            clear_global_registry()
