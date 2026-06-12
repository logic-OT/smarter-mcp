"""Tests for H2 — SSRF via image URL fetch.

- URL fetching disabled by default.
- Private / loopback / link-local IPs blocked when enabled.
- Local file reading disabled by default.
- No real network requests are made — DNS resolution is patched.
- C1: DNS-rebinding blocked via post-connect peer-IP check.
- C2: IPv4-mapped IPv6 addresses blocked.
- I3: Unparseable resolved address fails closed (blocked, not silently passed).
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest

from smarter_mcp.errors import CoercionError
from smarter_mcp.multimodal.interceptor import (
    _is_private_ip,
    _is_raw_ip_blocked,
    resolve_image_input,
)


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


class TestC1DnsRebindingBlocked:
    """C1: DNS-rebinding bypass must be blocked via post-connect peer-IP check.

    The pre-validation step calls getaddrinfo once.  An attacker with a TTL-0
    record can serve a public IP during validation and a private IP at connect
    time.  The fix validates sock.getpeername() AFTER the TCP connection, which
    the kernel guarantees to reflect the actual peer.
    """

    def test_rebind_blocked_at_connect_time(self):
        """Pre-validation sees public IP; mock socket reports private IP at connect → CoercionError.

        socket.getaddrinfo is patched so the first call (pre-validation in
        _assert_url_safe / _is_private_ip) returns a public IP that passes the
        SSRF check.  socket.create_connection is patched to return a mock socket
        whose getpeername() reports the private (rebound) IP.  Our
        _SSRFGuardedHTTPConnection.connect() detects this and raises CoercionError.
        """
        from smarter_mcp.config.manifest import MultimodalConfig

        cfg = MultimodalConfig(allow_url_fetch=True)
        call_count = [0]

        def fake_getaddrinfo(host, port, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call — pre-validation: public IP passes the SSRF check.
                return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 80))]
            # Any subsequent call simulates DNS rebinding to a private IP.
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", port or 80))]

        # Mock socket that reports the rebinding destination as its peer.
        mock_sock = MagicMock()
        mock_sock.getpeername.return_value = ("169.254.169.254", 80)

        with (
            patch(
                "smarter_mcp.multimodal.interceptor.socket.getaddrinfo",
                side_effect=fake_getaddrinfo,
            ),
            patch("socket.create_connection", return_value=mock_sock),
        ):
            with pytest.raises(CoercionError, match="SSRF"):
                resolve_image_input(
                    "http://rebind.example.com/image.png",
                    "pil.image.image",
                    cfg,
                )

    def test_public_ip_at_connect_time_is_allowed_through(self):
        """If both validation AND connect return a public IP, no CoercionError is raised
        at the SSRF-guard level (network errors are a different matter).
        """
        from smarter_mcp.config.manifest import MultimodalConfig

        cfg = MultimodalConfig(allow_url_fetch=True)

        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 80))]

        mock_sock = MagicMock()
        mock_sock.getpeername.return_value = ("93.184.216.34", 80)
        # Make the response object look like a minimal valid HTTP response so
        # _fetch_url_blocking can complete (content is minimal to avoid PIL decode).
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.url = "http://example.com/image.png"
        mock_resp.headers.get.return_value = "0"
        mock_resp.read.return_value = b"\x00" * 5

        with (
            patch(
                "smarter_mcp.multimodal.interceptor.socket.getaddrinfo",
                side_effect=fake_getaddrinfo,
            ),
            patch("socket.create_connection", return_value=mock_sock),
            patch(
                "smarter_mcp.multimodal.interceptor.urllib.request.OpenerDirector.open",
                return_value=mock_resp,
            ),
        ):
            # Should NOT raise CoercionError due to SSRF guard — the SSRF check passes.
            # (Actual PIL decode may fail on the dummy bytes, which is acceptable.)
            try:
                resolve_image_input(
                    "http://example.com/image.png",
                    "pil.image.image",
                    cfg,
                )
            except CoercionError as exc:
                assert "SSRF" not in str(exc), (
                    f"Should not raise SSRF CoercionError for public IP, got: {exc}"
                )


class TestC2IPv4MappedIPv6Blocked:
    """C2: IPv4-mapped IPv6 addresses must be blocked by the SSRF guard."""

    @pytest.mark.parametrize(
        "ip,label",
        [
            ("::1", "IPv6 loopback"),
            ("fd12:3456:789a::1", "IPv6 ULA"),
            ("fe80::1", "IPv6 link-local"),
            ("::ffff:169.254.169.254", "IPv4-mapped link-local / AWS metadata"),
            ("::ffff:127.0.0.1", "IPv4-mapped loopback"),
            ("::ffff:10.0.0.1", "IPv4-mapped RFC-1918"),
        ],
    )
    def test_ipv6_address_is_blocked(self, ip: str, label: str):
        """Every private / reserved IPv6 address (including mapped IPv4) must be blocked."""
        assert _is_raw_ip_blocked(ip) is True, (
            f"Expected {label} ({ip}) to be blocked, but it was not"
        )

    def test_public_ipv6_is_not_blocked(self):
        """A routable global unicast IPv6 address must not be blocked."""
        assert _is_raw_ip_blocked("2001:db8::1") is False

    def test_is_private_ip_rejects_mapped_loopback(self):
        """_is_private_ip must return True for ::ffff:127.0.0.1 resolved from a hostname."""
        mapped_loopback = ("::ffff:127.0.0.1", 80, 0, 0)
        with patch(
            "smarter_mcp.multimodal.interceptor.socket.getaddrinfo",
            return_value=[(socket.AF_INET6, socket.SOCK_STREAM, 0, "", mapped_loopback)],
        ):
            assert _is_private_ip("anyhost") is True

    def test_is_private_ip_rejects_mapped_link_local(self):
        """_is_private_ip must return True for ::ffff:169.254.169.254 resolved from hostname."""
        mapped_metadata = ("::ffff:169.254.169.254", 80, 0, 0)
        with patch(
            "smarter_mcp.multimodal.interceptor.socket.getaddrinfo",
            return_value=[(socket.AF_INET6, socket.SOCK_STREAM, 0, "", mapped_metadata)],
        ):
            assert _is_private_ip("anyhost") is True


class TestI3FailClosedOnUnparseableAddress:
    """I3: An unparseable resolved IP must be treated as blocked (fail closed)."""

    def test_raw_ip_blocked_raises_on_garbage(self):
        """_is_raw_ip_blocked must raise CoercionError on a non-IP string (fail closed)."""
        with pytest.raises(CoercionError, match="Cannot parse"):
            _is_raw_ip_blocked("not-an-ip-address")

    def test_is_private_ip_treats_garbage_as_private(self):
        """_is_private_ip must return True (blocked) when getaddrinfo returns an unparseable IP."""
        with patch(
            "smarter_mcp.multimodal.interceptor.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("garbage-value", 80))],
        ):
            assert _is_private_ip("anyhost") is True, (
                "Unparseable resolved address must be treated as private (fail closed)"
            )
