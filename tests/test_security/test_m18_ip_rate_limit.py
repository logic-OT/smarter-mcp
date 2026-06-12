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
