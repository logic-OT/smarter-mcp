"""
Multimodal input resolution and output coercion.

H2/H3/b64decode security hardening:
- URL fetching is OPT-IN (MultimodalConfig.allow_url_fetch=False by default).
- Local file reads are OPT-IN (MultimodalConfig.allow_local_file=False).
- When URL fetching is enabled:
  - Enforces a configurable timeout (url_fetch_timeout).
  - Resolves the hostname and rejects private/loopback/link-local ranges to
    prevent SSRF (10/8, 127/8, 169.254/16, 192.168/16, 172.16/12, ::1,
    metadata 169.254.169.254).
  - Caps bytes read to url_max_bytes (Content-Length + absolute ceiling).
  - Blocks redirects to private IPs.
  - Offloads blocking network I/O off the event loop via anyio.to_thread.
- PIL.Image.MAX_IMAGE_PIXELS is set to a safe bound to prevent decompression
  bombs (H3).
- Base64 payload length is capped before decoding (H3).
- base64.b64decode is called with validate=True so garbage strings fail fast
  with CoercionError rather than proceeding to a doomed PIL.open (b64decode
  minor).
- On any resolution failure, CoercionError is raised (not silent fallback).
"""

from __future__ import annotations

import binascii
import http.client
import io
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from smarter_mcp.config.manifest import MultimodalConfig

try:
    from fastmcp import Image
except ImportError:
    from fastmcp.utilities.types import Image  # type: ignore[no-redef]

# Lazy loaders
_PIL_AVAILABLE = None
_NUMPY_AVAILABLE = None

# H3: safe pixel cap — 89 million pixels matches Pillow's own default but we
# set it explicitly so our limit is not silently loosened by future Pillow
# releases that ship a higher default.
_SAFE_MAX_IMAGE_PIXELS = 89_478_485  # ~8192x8192 @ 4 bytes/pixel ~= 340 MB uncompressed


def _set_pil_pixel_limit() -> None:
    """H3: cap PIL decompression-bomb limit at module load time."""
    try:
        import PIL.Image
        PIL.Image.MAX_IMAGE_PIXELS = _SAFE_MAX_IMAGE_PIXELS
    except ImportError:
        pass


_set_pil_pixel_limit()


def is_pillow_available() -> bool:
    global _PIL_AVAILABLE
    if _PIL_AVAILABLE is None:
        try:
            import PIL.Image  # noqa: F401
            _PIL_AVAILABLE = True
        except ImportError:
            _PIL_AVAILABLE = False
    return _PIL_AVAILABLE


def is_numpy_available() -> bool:
    global _NUMPY_AVAILABLE
    if _NUMPY_AVAILABLE is None:
        try:
            import numpy  # noqa: F401
            _NUMPY_AVAILABLE = True
        except ImportError:
            _NUMPY_AVAILABLE = False
    return _NUMPY_AVAILABLE


# ──────────────────────────────────────────────────────────────────────
# SSRF guard
# ──────────────────────────────────────────────────────────────────────

# Private / reserved network ranges that must never be reached via SSRF.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / AWS metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 ULA
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
    # C2: IPv4-mapped IPv6 block covers ::ffff:10.x.x.x, ::ffff:127.x.x.x,
    # ::ffff:169.254.x.x, etc. without requiring per-address unwrapping here.
    ipaddress.ip_network("::ffff:0:0/96"),
]


def _is_raw_ip_blocked(ip_str: str) -> bool:
    """Return True if *ip_str* (a resolved IP address string) is in a blocked range.

    Does NOT perform DNS resolution — *ip_str* must already be a dotted-decimal
    IPv4 address or colon-hex IPv6 address.

    C2: IPv4-mapped IPv6 addresses (e.g. ``::ffff:169.254.169.254``) are
    unwrapped and their IPv4 component is also checked against the IPv4 blocked
    networks, in addition to the ``::ffff:0:0/96`` entry in ``_BLOCKED_NETWORKS``.

    I3: Fails CLOSED — if *ip_str* cannot be parsed, ``CoercionError`` is raised
    rather than silently allowing the address through.
    """
    from smarter_mcp.errors import CoercionError

    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        # I3: treat unparseable resolved address as blocked (fail closed).
        raise CoercionError(
            f"Cannot parse resolved IP address {ip_str!r}: treating as blocked"
        ) from None

    # C2: unwrap IPv4-mapped IPv6 and check the mapped IPv4 against IPv4 nets.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        mapped = addr.ipv4_mapped
        for network in _BLOCKED_NETWORKS:
            if network.version == 4 and mapped in network:
                return True

    for network in _BLOCKED_NETWORKS:
        if addr in network:
            return True
    return False


def _is_private_ip(host: str) -> bool:
    """Return True if *host* resolves to a private/reserved IP address.

    Raises ``CoercionError`` on unresolvable hostnames so we always fail
    closed rather than passing through to an unknown destination.

    I3: an address that cannot be parsed by ``ipaddress.ip_address`` is treated
    as private/blocked (fail closed) rather than silently skipped.
    C2: IPv4-mapped IPv6 addresses are also checked (see ``_is_raw_ip_blocked``).
    """
    from smarter_mcp.errors import CoercionError

    try:
        # getaddrinfo returns a list of (family, type, proto, canonname, sockaddr)
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise CoercionError(
            f"Cannot resolve hostname '{host}' for image URL fetch: {exc}"
        ) from exc

    for info in infos:
        sockaddr = info[4]
        raw_ip = sockaddr[0]
        try:
            blocked = _is_raw_ip_blocked(raw_ip)
        except CoercionError:
            # I3: unparseable resolved address → fail closed (treat as private).
            return True
        if blocked:
            return True
    return False


def _assert_url_safe(url: str) -> None:
    """Parse *url*, resolve its host, and raise ``CoercionError`` if it targets
    a private/reserved IP range.

    H2: called before every URL fetch to prevent SSRF.
    """
    from smarter_mcp.errors import CoercionError

    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if not host:
        raise CoercionError(f"Cannot parse host from URL: {url!r}")
    if _is_private_ip(host):
        raise CoercionError(
            f"URL fetch to '{host}' is blocked: private/reserved IP range (SSRF guard)"
        )


# ──────────────────────────────────────────────────────────────────────
# Anti-DNS-rebinding: validate peer IP at connect time (C1)
# ──────────────────────────────────────────────────────────────────────

class _SSRFGuardedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection that validates the peer IP after the TCP handshake.

    C1 (DNS-rebinding / TOCTOU): ``_assert_url_safe`` checks DNS before
    ``urlopen``; this class closes the window between that check and the actual
    TCP connection by reading ``sock.getpeername()`` once the connection is
    established and rejecting it if the peer IP is in a private/blocked range.
    The kernel cannot lie about ``getpeername`` — it reflects the TCP state and
    is immutable for the lifetime of the connection.
    """

    def connect(self) -> None:  # type: ignore[override]
        from smarter_mcp.errors import CoercionError

        super().connect()
        peer_ip = self.sock.getpeername()[0]
        try:
            blocked = _is_raw_ip_blocked(peer_ip)
        except CoercionError:
            self.sock.close()
            raise CoercionError(
                f"SSRF blocked: connected IP {peer_ip!r} could not be validated "
                "(treated as blocked)"
            ) from None
        if blocked:
            self.sock.close()
            raise CoercionError(
                f"SSRF blocked: connected IP {peer_ip!r} is in a private/reserved range"
            )


class _SSRFGuardedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that validates the peer IP after the TLS handshake.

    See ``_SSRFGuardedHTTPConnection`` — same rationale, same mechanism.
    ``getpeername()`` on an SSL-wrapped socket returns the TCP-level peer
    address (which is immutable), so this check is equally reliable.
    """

    def connect(self) -> None:  # type: ignore[override]
        from smarter_mcp.errors import CoercionError

        super().connect()
        peer_ip = self.sock.getpeername()[0]
        try:
            blocked = _is_raw_ip_blocked(peer_ip)
        except CoercionError:
            self.sock.close()
            raise CoercionError(
                f"SSRF blocked: connected IP {peer_ip!r} could not be validated "
                "(treated as blocked)"
            ) from None
        if blocked:
            self.sock.close()
            raise CoercionError(
                f"SSRF blocked: connected IP {peer_ip!r} is in a private/reserved range"
            )


def _make_ssrf_guarded_opener() -> urllib.request.OpenerDirector:
    """Build a urllib opener using the anti-rebind connection classes.

    C1: The custom handlers pass ``_SSRFGuardedHTTP(S)Connection`` to
    ``do_open`` so every connection goes through the post-connect peer-IP check.
    """

    class _GuardedHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):  # type: ignore[override]
            return self.do_open(_SSRFGuardedHTTPConnection, req)

    class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):  # type: ignore[override]
            return self.do_open(_SSRFGuardedHTTPSConnection, req)

    return urllib.request.build_opener(
        _GuardedHTTPHandler(),
        _GuardedHTTPSHandler(),
    )


# ──────────────────────────────────────────────────────────────────────
# Blocking fetch helper (called via anyio.to_thread for async callers)
# ──────────────────────────────────────────────────────────────────────

def _fetch_url_blocking(url: str, timeout: float, max_bytes: int) -> bytes:
    """Fetch *url* synchronously with a timeout and size cap.

    This function is intentionally synchronous so it can be offloaded to a
    thread via ``anyio.to_thread.run_sync`` for async callers (H2 / M2).

    H2 / C1: SSRF guard is called before every fetch (pre-validation) AND the
    connection is made through ``_make_ssrf_guarded_opener()`` which re-checks
    the peer IP at connect time via ``sock.getpeername()``. This two-layer check
    defeats DNS-rebinding attacks where the attacker's TTL-0 record resolves to
    a public IP during validation but a private IP during the actual connection.
    """
    from smarter_mcp.errors import CoercionError

    # Pre-validation: catches the common case and avoids connecting at all.
    _assert_url_safe(url)

    # C1: Use the guarded opener so the peer IP is validated at connect time,
    # not just at pre-validation time (defeats DNS rebinding).
    opener = _make_ssrf_guarded_opener()
    req = urllib.request.Request(url, headers={"User-Agent": "smarter-mcp/image-fetch"})  # noqa: S310 — URL pre-validated by _assert_url_safe + SSRF-guarded opener
    try:
        with opener.open(req, timeout=timeout) as resp:
            # Re-validate after potential redirect.
            final_url = resp.url or url
            final_parsed = urllib.parse.urlparse(final_url)
            orig_host = urllib.parse.urlparse(url).hostname
            if final_parsed.hostname and final_parsed.hostname != orig_host:
                _assert_url_safe(final_url)

            # H3: respect Content-Length but never trust it — cap unconditionally.
            content_length = int(resp.headers.get("Content-Length", 0) or 0)
            # M4: rewrite the chained comparison to unambiguous form.
            if content_length > 0 and content_length > max_bytes:
                raise CoercionError(
                    f"Image URL response too large: Content-Length {content_length} "
                    f"exceeds limit {max_bytes}"
                )

            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise CoercionError(
                    f"Image URL response exceeds maximum allowed size of {max_bytes} bytes"
                )
            return data
    except urllib.error.URLError as exc:
        raise CoercionError(f"Failed to fetch image URL '{url}': {exc}") from exc


# ──────────────────────────────────────────────────────────────────────
# Safe base64 decode
# ──────────────────────────────────────────────────────────────────────

def _safe_b64decode(data: str, max_bytes: int) -> bytes:
    """Decode a base64 string with validation and a size cap (H3 + b64decode).

    Args:
        data: Base64-encoded string.
        max_bytes: Maximum decoded byte count allowed.

    Raises:
        CoercionError: If the string is not valid base64 or the decoded size
            exceeds *max_bytes*.
    """
    import base64

    from smarter_mcp.errors import CoercionError

    # H3: cap encoded length before decode.  A base64 string of length N encodes
    # at most ~0.75 * N bytes, so if the encoded length already exceeds
    # max_bytes * 1.4 the decoded output will certainly be too large.
    if len(data) > max_bytes * 1.4 + 4:
        raise CoercionError(
            f"Base64 payload too large: encoded length {len(data)} would decode "
            f"to more than {max_bytes} bytes"
        )

    try:
        # b64decode minor: validate=True rejects non-base64 characters immediately
        # rather than ignoring them and proceeding to a broken PIL.open.
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CoercionError(f"Invalid base64 data: {exc}") from exc

    if len(decoded) > max_bytes:
        raise CoercionError(
            f"Decoded image data exceeds maximum allowed size of {max_bytes} bytes"
        )
    return decoded


# ──────────────────────────────────────────────────────────────────────
# Image size enforcement
# ──────────────────────────────────────────────────────────────────────

def _enforce_image_size(img: Any, max_size: tuple[int, int]) -> Any:
    """H3: reject images that exceed *max_size* (width, height) in pixels.

    Raises CoercionError rather than silently resizing — callers that need
    resizing should do so explicitly before sending the image parameter.
    """
    from smarter_mcp.errors import CoercionError

    w, h = img.size
    max_w, max_h = max_size
    if w > max_w or h > max_h:
        raise CoercionError(
            f"Image dimensions {w}x{h} exceed configured maximum {max_w}x{max_h}. "
            "Resize the image before sending it as a tool parameter."
        )
    return img


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────

def resolve_image_input(
    val: Any,
    target_type: str,
    config: MultimodalConfig | None = None,
) -> Any:
    """Resolve the input value and load it into the target image type.

    Accepts: MCP ImageContent dict, data URL, HTTP(S) URL (opt-in), local path
    (opt-in), or raw base64 string.

    H2: URL fetching and local file access are disabled by default.
    H3: image size and byte payload are capped.
    b64decode: validation=True used throughout.

    Args:
        val: The raw parameter value from the MCP call.
        target_type: The target Python type as a lowercase string
            (e.g. "pil.image.image", "numpy.ndarray").
        config: Optional MultimodalConfig carrying security limits.  When None,
            defaults are applied (URL fetch OFF, local file OFF, 10 MB cap,
            1024x1024 pixel limit).

    Returns:
        A PIL.Image.Image or numpy.ndarray ready for the tool function.

    Raises:
        CoercionError: On any resolution failure, size violation, or
            when attempting a disabled operation (URL fetch / local file).
    """
    from smarter_mcp.errors import CoercionError

    # Pull config values (with safe defaults when config is absent).
    allow_url_fetch: bool = getattr(config, "allow_url_fetch", False)
    allow_local_file: bool = getattr(config, "allow_local_file", False)
    url_timeout: float = getattr(config, "url_fetch_timeout", 10.0)
    max_bytes: int = getattr(config, "url_max_bytes", 10 * 1024 * 1024)
    image_max_size: tuple[int, int] = getattr(config, "image_max_size", (1024, 1024))

    data: bytes | None = None

    if isinstance(val, dict):
        # Handle MCP ImageContent dict: {"type":"image","data":"...","mimeType":"..."}
        if "data" in val:
            data = _safe_b64decode(val["data"], max_bytes)
        elif "path" in val:
            val = val["path"]
        elif "url" in val:
            val = val["url"]

    if data is None and isinstance(val, str):
        # ── Data URL: "data:image/png;base64,iVBOR..." ──────────────────────
        if val.startswith("data:image/") and "base64," in val:
            parts = val.split("base64,", 1)
            if len(parts) == 2:
                data = _safe_b64decode(parts[1], max_bytes)
            else:
                raise CoercionError("Malformed data URL: missing base64 payload")
        else:
            parsed = urllib.parse.urlparse(val)
            if parsed.scheme in ("http", "https"):
                # ── HTTP/HTTPS URL fetch (H2: OPT-IN) ──────────────────────
                if not allow_url_fetch:
                    raise CoercionError(
                        "Image URL fetching is disabled. Set "
                        "multimodal.allow_url_fetch=true in your manifest to enable it."
                    )
                # Sync callers: fetch directly.  Async callers should use
                # resolve_image_input_async() which offloads to a thread.
                data = _fetch_url_blocking(val, url_timeout, max_bytes)

            elif parsed.scheme in ("", "file") and not parsed.netloc:
                # ── Local filesystem path (H2: OPT-IN) ─────────────────────
                if not allow_local_file:
                    raise CoercionError(
                        "Local file image loading is disabled. Set "
                        "multimodal.allow_local_file=true in your manifest to enable it."
                    )
                path = Path(val)
                if not path.is_file():
                    raise CoercionError(
                        f"Image file not found or not a regular file: '{val}'"
                    )
                try:
                    raw = path.read_bytes()
                except OSError as exc:
                    raise CoercionError(f"Cannot read image file '{val}': {exc}") from exc
                if len(raw) > max_bytes:
                    raise CoercionError(
                        f"Image file '{val}' ({len(raw)} bytes) exceeds maximum "
                        f"allowed size of {max_bytes} bytes"
                    )
                data = raw
            else:
                # ── Try raw base64 as last resort ───────────────────────────
                try:
                    data = _safe_b64decode(val, max_bytes)
                except CoercionError:
                    raise CoercionError(
                        f"Cannot resolve image input: not a valid URL, file path, "
                        f"or base64 string (scheme={parsed.scheme!r})"
                    ) from None

    if data is None:
        raise CoercionError(
            f"Could not resolve image input of type {type(val).__name__!r}. "
            "Expected an MCP ImageContent dict, data URL, HTTP URL, local path "
            "(when enabled), or base64 string."
        )

    # ── Decode bytes into target type ──────────────────────────────────────
    target_lower = target_type.lower()

    if "pil.image" in target_lower or "image.image" in target_lower:
        if not is_pillow_available():
            raise ImportError(
                "Pillow is required for this tool. "
                "Install smarter-mcp[multimodal]."
            )
        import PIL.Image
        try:
            img = PIL.Image.open(io.BytesIO(data)).copy()
        except Exception as exc:
            raise CoercionError(f"Failed to decode image data: {exc}") from exc
        # H3: enforce configured pixel limits.
        return _enforce_image_size(img, image_max_size)

    elif "numpy.ndarray" in target_lower or "ndarray" in target_lower:
        if not is_numpy_available() or not is_pillow_available():
            raise ImportError(
                "Pillow and numpy are required for this tool. "
                "Install smarter-mcp[multimodal]."
            )
        import numpy as np
        import PIL.Image
        try:
            img = PIL.Image.open(io.BytesIO(data))
        except Exception as exc:
            raise CoercionError(f"Failed to decode image data: {exc}") from exc
        _enforce_image_size(img, image_max_size)
        return np.array(img)

    raise CoercionError(
        f"Unsupported image target type: {target_type!r}. "
        "Expected PIL.Image.Image or numpy.ndarray."
    )


async def resolve_image_input_async(
    val: Any,
    target_type: str,
    config: MultimodalConfig | None = None,
) -> Any:
    """Async version of ``resolve_image_input``.

    For HTTP URL fetches, the blocking network I/O is offloaded to a thread
    via ``anyio.to_thread.run_sync`` so the event loop is never blocked
    (H2 / M2 fix).  All other paths (base64 decode, PIL open) are fast enough
    to run inline.
    """
    import urllib.parse as _urlparse

    from smarter_mcp.errors import CoercionError

    allow_url_fetch: bool = getattr(config, "allow_url_fetch", False)
    url_timeout: float = getattr(config, "url_fetch_timeout", 10.0)
    max_bytes: int = getattr(config, "url_max_bytes", 10 * 1024 * 1024)

    # For HTTP URLs, do the blocking part in a thread first, then proceed to
    # image decoding synchronously.
    if isinstance(val, str):
        parsed = _urlparse.urlparse(val)
        if parsed.scheme in ("http", "https"):
            if not allow_url_fetch:
                raise CoercionError(
                    "Image URL fetching is disabled. Set "
                    "multimodal.allow_url_fetch=true in your manifest to enable it."
                )
            import anyio.to_thread as _to_thread

            def _fetch() -> bytes:
                return _fetch_url_blocking(val, url_timeout, max_bytes)

            data_bytes = await _to_thread.run_sync(_fetch)
            # Reconstruct as a dict-style ImageContent so the synchronous
            # resolver handles PIL decode + size enforcement.
            return resolve_image_input(
                {"data": __import__("base64").b64encode(data_bytes).decode()},
                target_type,
                config,
            )

    # All non-HTTP paths are fast enough to resolve synchronously.
    return resolve_image_input(val, target_type, config)


def coerce_to_fastmcp_image(val: Any) -> Any:
    """Convert a genuine image value into a fastmcp.Image.

    Only PIL.Image.Image instances and NumPy arrays are converted; an already-
    wrapped fastmcp.Image is returned as-is.  All other types (str, bytes, Path,
    int, dict, …) are returned unchanged so that ordinary tool return values
    pass through the MCP wire protocol without modification.

    Rationale for removing the str/bytes/Path branches: treating every string
    as an image file-path breaks every tool that returns plain text (the string
    is handed to fastmcp.Image(path=…) and fails with FileNotFoundError when the
    string is not a valid path).  Image data in those forms must be explicitly
    wrapped by the tool author.
    """
    if isinstance(val, Image):
        return val

    if is_pillow_available():
        import PIL.Image
        if isinstance(val, PIL.Image.Image):
            buf = io.BytesIO()
            val.save(buf, format="PNG")
            return Image(data=buf.getvalue(), format="png")

    if is_numpy_available() and is_pillow_available():
        import numpy as np
        import PIL.Image
        if isinstance(val, np.ndarray):
            # Convert NumPy array to PIL Image first
            if val.dtype != np.uint8:
                # Basic normalization if float
                if val.dtype in (np.float32, np.float64):
                    val = (val * 255).astype(np.uint8)
                else:
                    val = val.astype(np.uint8)
            img = PIL.Image.fromarray(val)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return Image(data=buf.getvalue(), format="png")

    # Not a recognised image type — return unchanged.
    return val
