"""Tests for H3 — decompression bomb and image size limits, and b64decode minor.

- image_max_size is enforced (previously dead config).
- Oversized base64 payloads are rejected before decode.
- Invalid base64 raises CoercionError (validate=True).
- PIL.Image.MAX_IMAGE_PIXELS is set to a safe bound at import time.
"""

from __future__ import annotations

import base64
import io

import pytest

from smarter_mcp.errors import CoercionError
from smarter_mcp.multimodal.interceptor import (
    _SAFE_MAX_IMAGE_PIXELS,
    _safe_b64decode,
    resolve_image_input,
)


class TestPilPixelLimit:
    def test_pil_max_image_pixels_is_capped(self):
        """PIL.Image.MAX_IMAGE_PIXELS must be set to our safe bound at import."""
        try:
            import PIL.Image

            assert PIL.Image.MAX_IMAGE_PIXELS is not None
            assert PIL.Image.MAX_IMAGE_PIXELS <= _SAFE_MAX_IMAGE_PIXELS, (
                f"Expected PIL.Image.MAX_IMAGE_PIXELS <= {_SAFE_MAX_IMAGE_PIXELS}, "
                f"got {PIL.Image.MAX_IMAGE_PIXELS}"
            )
        except ImportError:
            pytest.skip("Pillow not installed")


class TestB64DecodeValidation:
    """b64decode must use validate=True — garbage strings must fail fast."""

    def test_invalid_base64_raises_coercion_error(self):
        with pytest.raises(CoercionError, match="Invalid base64"):
            _safe_b64decode("not!!valid!!base64!!!", max_bytes=10 * 1024 * 1024)

    def test_valid_base64_decoded_correctly(self):
        raw = b"hello world"
        encoded = base64.b64encode(raw).decode()
        result = _safe_b64decode(encoded, max_bytes=1024)
        assert result == raw

    def test_base64_with_padding_works(self):
        # Standard padded base64
        raw = b"\x00\x01\x02\x03"
        encoded = base64.b64encode(raw).decode()
        result = _safe_b64decode(encoded, max_bytes=1024)
        assert result == raw


class TestBase64SizeCap:
    """Oversized base64 payloads must be rejected before decoding."""

    def test_oversized_encoded_string_rejected(self):
        # 1 MB limit; construct an encoded string that would decode to > 1 MB
        limit = 1024  # 1 KB for this test
        # A string of length > limit * 1.4 + 4 will be rejected
        long_b64 = "A" * (limit * 2 + 100)
        with pytest.raises(CoercionError, match="too large"):
            _safe_b64decode(long_b64, max_bytes=limit)

    def test_decoded_size_cap_enforced(self):
        # Encode exactly limit+1 bytes of zeros
        limit = 100
        raw = b"\x00" * (limit + 1)
        encoded = base64.b64encode(raw).decode()
        with pytest.raises(CoercionError, match="exceeds maximum"):
            _safe_b64decode(encoded, max_bytes=limit)


class TestImageMaxSizeEnforcement:
    """image_max_size from config must actually reject oversized images."""

    def _make_png_bytes(self, width: int, height: int) -> bytes:
        """Create a minimal valid PNG of given dimensions."""
        try:
            import PIL.Image

            img = PIL.Image.new("RGB", (width, height), color=(255, 0, 0))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except ImportError:
            pytest.skip("Pillow not installed")

    def _to_data_url(self, png_bytes: bytes) -> str:
        encoded = base64.b64encode(png_bytes).decode()
        return f"data:image/png;base64,{encoded}"

    def test_image_within_limit_accepted(self):
        from smarter_mcp.config.manifest import MultimodalConfig

        png = self._make_png_bytes(100, 100)
        cfg = MultimodalConfig(image_max_size=(200, 200))
        result = resolve_image_input(
            self._to_data_url(png), "pil.image.image", cfg
        )
        assert result is not None

    def test_image_exceeding_width_rejected(self):
        from smarter_mcp.config.manifest import MultimodalConfig

        png = self._make_png_bytes(300, 100)
        cfg = MultimodalConfig(image_max_size=(200, 200))
        with pytest.raises(CoercionError, match="dimensions"):
            resolve_image_input(self._to_data_url(png), "pil.image.image", cfg)

    def test_image_exceeding_height_rejected(self):
        from smarter_mcp.config.manifest import MultimodalConfig

        png = self._make_png_bytes(100, 400)
        cfg = MultimodalConfig(image_max_size=(200, 200))
        with pytest.raises(CoercionError, match="dimensions"):
            resolve_image_input(self._to_data_url(png), "pil.image.image", cfg)
