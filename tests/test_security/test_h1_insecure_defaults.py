"""Tests for H1 — insecure defaults.

H1: ServerConfig default host must be 127.0.0.1 (not 0.0.0.0).
    A startup WARNING must fire when binding to a non-loopback host with
    auth_enabled=False.
"""

from __future__ import annotations

import logging

from smarter_mcp.config.manifest import ManifestConfig, ServerConfig
from smarter_mcp.server.app import _warn_insecure_bind


class TestH1DefaultHost:
    def test_default_host_is_loopback(self):
        cfg = ServerConfig()
        assert cfg.host == "127.0.0.1", (
            f"Default host must be 127.0.0.1, got {cfg.host!r}"
        )

    def test_manifest_default_host_is_loopback(self):
        manifest = ManifestConfig()
        assert manifest.server.host == "127.0.0.1"

    def test_explicit_override_works(self):
        cfg = ServerConfig(host="0.0.0.0")
        assert cfg.host == "0.0.0.0"


class TestH1InsecureBindWarning:
    def test_no_warning_on_loopback(self, caplog):
        cfg = ServerConfig(host="127.0.0.1", auth_enabled=False)
        with caplog.at_level(logging.WARNING, logger="smarter_mcp.server.app"):
            _warn_insecure_bind(cfg)
        assert not any("SECURITY WARNING" in r.message for r in caplog.records)

    def test_no_warning_when_auth_enabled_on_public_host(self, caplog):
        # Non-loopback + auth enabled → no warning needed
        cfg = ServerConfig(host="0.0.0.0", auth_enabled=True)
        with caplog.at_level(logging.WARNING, logger="smarter_mcp.server.app"):
            _warn_insecure_bind(cfg)
        assert not any("SECURITY WARNING" in r.message for r in caplog.records)

    def test_warning_fires_on_public_host_without_auth(self, caplog):
        cfg = ServerConfig(host="0.0.0.0", auth_enabled=False)
        with caplog.at_level(logging.WARNING, logger="smarter_mcp.server.app"):
            _warn_insecure_bind(cfg)
        messages = [r.message for r in caplog.records]
        assert any("SECURITY WARNING" in m for m in messages), (
            "Expected a SECURITY WARNING log when binding 0.0.0.0 without auth. "
            f"Got: {messages}"
        )

    def test_warning_fires_on_non_loopback_ipv4(self, caplog):
        cfg = ServerConfig(host="192.168.1.10", auth_enabled=False)
        with caplog.at_level(logging.WARNING, logger="smarter_mcp.server.app"):
            _warn_insecure_bind(cfg)
        messages = [r.message for r in caplog.records]
        assert any("SECURITY WARNING" in m for m in messages)
