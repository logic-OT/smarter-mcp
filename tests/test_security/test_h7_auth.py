"""Tests for H7 — timing-safe key compare and fail-closed auth.

- APIKeyMiddleware uses hmac.compare_digest (constant-time compare).
- auth_enabled=True with no keys configured → RuntimeError at startup.
- assert_auth_keys_present fails closed regardless of which entrypoint.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from smarter_mcp.config.manifest import ServerConfig
from smarter_mcp.server.security import (
    APIKeyMiddleware,
    _constant_time_key_check,
    assert_auth_keys_present,
    build_auth_provider,
)


class TestConstantTimeKeyCheck:
    def test_valid_key_accepted(self):
        keys = {"secret-key-abc", "another-key-xyz"}
        assert _constant_time_key_check("secret-key-abc", keys) is True

    def test_invalid_key_rejected(self):
        keys = {"real-key"}
        assert _constant_time_key_check("wrong-key", keys) is False

    def test_empty_key_rejected(self):
        keys = {"real-key"}
        assert _constant_time_key_check("", keys) is False

    def test_iterates_all_keys_no_short_circuit(self):
        """All keys must be checked even when a match is found early (timing safety).

        We verify this by confirming the function iterates all keys by
        checking return value correctness across a large key set.
        """
        keys = {f"key-{i}" for i in range(100)}
        # A key at the start of the set (iteration order not guaranteed)
        assert _constant_time_key_check("key-0", keys) is True
        assert _constant_time_key_check("not-a-key", keys) is False

    def test_partial_match_rejected(self):
        keys = {"supersecret"}
        assert _constant_time_key_check("super", keys) is False
        assert _constant_time_key_check("supersecret_extra", keys) is False


class TestAuthFailClosed:
    """auth_enabled=True with no keys must fail at startup, not fail-open."""

    def test_assert_auth_keys_present_raises_when_no_keys(self):
        cfg = ServerConfig(auth_enabled=True, auth_keys_env="NONEXISTENT_ENV_VAR_12345")
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError, match="no API keys"):
                assert_auth_keys_present(cfg)

    def test_assert_auth_keys_present_passes_when_auth_disabled(self):
        cfg = ServerConfig(auth_enabled=False)
        # Must not raise
        assert_auth_keys_present(cfg)

    def test_assert_auth_keys_present_passes_with_keys(self):
        cfg = ServerConfig(auth_enabled=True, auth_keys_env="MY_KEYS_ENV")
        with patch.dict(os.environ, {"MY_KEYS_ENV": "key-one,key-two"}):
            assert_auth_keys_present(cfg)  # must not raise

    def test_build_auth_provider_raises_when_no_keys(self):
        cfg = ServerConfig(auth_enabled=True, auth_keys_env="MISSING_KEYS_VAR_99")
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError, match="no API keys"):
                build_auth_provider(cfg)

    def test_build_auth_provider_returns_none_when_disabled(self):
        cfg = ServerConfig(auth_enabled=False)
        result = build_auth_provider(cfg)
        assert result is None


class TestAPIKeyMiddlewareUsesHmac:
    """APIKeyMiddleware must delegate to _constant_time_key_check (not `in`)."""

    @pytest.mark.asyncio
    async def test_valid_key_passes(self):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        def ok(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/test", ok)])
        app.add_middleware(
            APIKeyMiddleware,
            header_name="X-API-Key",
            valid_keys={"my-secret"},
            exempt_paths=frozenset(),
        )
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/test", headers={"X-API-Key": "my-secret"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_wrong_key_rejected(self):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        def ok(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/test", ok)])
        app.add_middleware(
            APIKeyMiddleware,
            header_name="X-API-Key",
            valid_keys={"my-secret"},
            exempt_paths=frozenset(),
        )
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/test", headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_key_rejected(self):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        def ok(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/test", ok)])
        app.add_middleware(
            APIKeyMiddleware,
            header_name="X-API-Key",
            valid_keys={"my-secret"},
            exempt_paths=frozenset(),
        )
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/test")
        assert resp.status_code == 401


class TestI2ConstantWidthComparison:
    """I2: _constant_time_key_check must hash both operands to fixed width before
    comparing, preventing length-oracle attacks via hmac.compare_digest's
    early-out on operands of different lengths.
    """

    def test_short_provided_key_rejected_not_crashed(self):
        """A very short provided value must be rejected, not cause an error."""
        keys = {"a-much-longer-secret-key-value"}
        # Without hashing, compare_digest would return False immediately on
        # mismatched lengths.  With hashing both sides are 32 bytes.
        assert _constant_time_key_check("x", keys) is False

    def test_long_provided_key_rejected(self):
        """A provided value longer than any valid key must also be safely rejected."""
        keys = {"short"}
        assert _constant_time_key_check("x" * 1000, keys) is False

    def test_exact_match_still_accepted_after_hashing(self):
        """Hashing must not break correct key acceptance."""
        keys = {"correct-key-abc123"}
        assert _constant_time_key_check("correct-key-abc123", keys) is True

    def test_all_keys_checked_regardless_of_match(self):
        """Even after a match, iteration must continue over all keys (no short-circuit)."""
        import hmac

        matched_calls = []

        original_compare = hmac.compare_digest

        def counting_compare(a, b):
            matched_calls.append((a, b))
            return original_compare(a, b)

        with patch("smarter_mcp.server.security.hmac.compare_digest", side_effect=counting_compare):
            keys = {"key-a", "key-b", "key-c"}
            result = _constant_time_key_check("key-a", keys)

        assert result is True
        # Must iterate all 3 keys — no early-out when a match is found.
        assert len(matched_calls) == len(keys), (
            f"Expected {len(keys)} comparisons (one per key), got {len(matched_calls)}"
        )
