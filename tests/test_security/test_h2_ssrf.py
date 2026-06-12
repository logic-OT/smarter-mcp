"""Tests for H2 — SSRF via image URL fetch.

- URL fetching disabled by default.
- Private / loopback / link-local IPs blocked when enabled.
- Local file reading disabled by default.
- No real network requests are made — DNS resolution is patched.
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from smarter_mcp.errors import CoercionError
from smarter_mcp.multimodal.interceptor import _is_private_ip, resolve_image_input


class TestUrlFetchDisabledByDefault:
    """URL fetching must be off unless explicitly opted in."""

    def test_http_url_raises_without_opt_in(self):
        """Sending an HTTP URL as an image parameter must raise CoercionError by default."""
        with pytest.raises(CoercionError, match="allow_url_fetch"):
            resolve_image_input(
                "http://example.com/image.png",
                "pil.image.image",
                config=None,  # default: allow_url_fetch=False
            )

    def test_https_url_raises_without_opt_in(self):
        with pytest.raises(CoercionError, match="allow_url_fetch"):
            resolve_image_input(
                "https://example.com/photo.jpg",
                "pil.image.image",
            )

    def test_http_url_raises_with_explicit_false(self):
        """Even an explicitly constructed config with allow_url_fetch=False must block."""
        from smarter_mcp.config.manifest import MultimodalConfig

        cfg = MultimodalConfig(allow_url_fetch=False)
        with pytest.raises(CoercionError, match="allow_url_fetch"):
            resolve_image_input("http://internal.host/img.png", "pil.image.image", cfg)


class TestLocalFileDisabledByDefault:
    """Local file reads must be off unless explicitly opted in."""

    def test_local_path_raises_without_opt_in(self, tmp_path):
        img_path = tmp_path / "test.png"
        img_path.write_bytes(b"fake")
        with pytest.raises(CoercionError, match="allow_local_file"):
            resolve_image_input(str(img_path), "pil.image.image", config=None)

    def test_local_path_raises_with_explicit_false(self, tmp_path):
        from smarter_mcp.config.manifest import MultimodalConfig

        img_path = tmp_path / "image.png"
        img_path.write_bytes(b"fake")
        cfg = MultimodalConfig(allow_local_file=False)
        with pytest.raises(CoercionError, match="allow_local_file"):
            resolve_image_input(str(img_path), "pil.image.image", cfg)


class TestPrivateIpBlocking:
    """Private/loopback/link-local IPs must be rejected even when URL fetch is enabled."""

    def _mock_getaddrinfo(self, ip: str):
        """Patch socket.getaddrinfo so we never make real DNS queries."""
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 80))]

    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",       # loopback
            "10.0.0.1",        # RFC 1918
            "172.16.0.1",      # RFC 1918
            "192.168.1.1",     # RFC 1918
            "169.254.169.254", # AWS metadata / link-local
            "169.254.0.1",     # other link-local
        ],
    )
    def test_private_ip_is_blocked(self, ip):
        with patch("smarter_mcp.multimodal.interceptor.socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = self._mock_getaddrinfo(ip)
            assert _is_private_ip("private.host") is True

    def test_public_ip_is_not_blocked(self):
        with patch("smarter_mcp.multimodal.interceptor.socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = self._mock_getaddrinfo("93.184.216.34")  # example.com
            assert _is_private_ip("example.com") is False

    def test_ssrf_blocked_on_private_ip_when_url_fetch_enabled(self):
        """Even with allow_url_fetch=True, private IPs must be rejected."""
        from smarter_mcp.config.manifest import MultimodalConfig

        cfg = MultimodalConfig(allow_url_fetch=True)

        with patch("smarter_mcp.multimodal.interceptor.socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 80))
            ]
            with pytest.raises(CoercionError, match="SSRF"):
                resolve_image_input(
                    "http://internal.aws/latest/meta-data/",
                    "pil.image.image",
                    cfg,
                )

    def test_ssrf_blocked_on_loopback_when_url_fetch_enabled(self):
        from smarter_mcp.config.manifest import MultimodalConfig

        cfg = MultimodalConfig(allow_url_fetch=True)

        with patch("smarter_mcp.multimodal.interceptor.socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 8080))
            ]
            with pytest.raises(CoercionError, match="SSRF"):
                resolve_image_input(
                    "http://localhost:8080/secret",
                    "pil.image.image",
                    cfg,
                )
