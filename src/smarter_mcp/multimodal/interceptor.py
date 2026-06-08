import io
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any

try:
    from fastmcp import Image
except ImportError:
    from fastmcp.utilities.types import Image

# Lazy loaders
_PIL_AVAILABLE = None
_NUMPY_AVAILABLE = None

def is_pillow_available() -> bool:
    global _PIL_AVAILABLE
    if _PIL_AVAILABLE is None:
        try:
            import PIL.Image
            _PIL_AVAILABLE = True
        except ImportError:
            _PIL_AVAILABLE = False
    return _PIL_AVAILABLE

def is_numpy_available() -> bool:
    global _NUMPY_AVAILABLE
    if _NUMPY_AVAILABLE is None:
        try:
            import numpy
            _NUMPY_AVAILABLE = True
        except ImportError:
            _NUMPY_AVAILABLE = False
    return _NUMPY_AVAILABLE

def resolve_image_input(val: Any, target_type: str) -> Any:
    """
    Resolves the input value (path, URL, base64 string, data URL, or dict/object)
    and loads it into the target type (PIL.Image.Image or numpy.ndarray).
    """
    import base64
    import binascii

    data = None

    if isinstance(val, dict):
        # Handle MCP ImageContent dict format: e.g. {"type": "image", "data": "...", "mimeType": "..."}
        if "data" in val:
            try:
                data = base64.b64decode(val["data"])
            except Exception:
                pass
        elif "path" in val:
            val = val["path"]
        elif "url" in val:
            val = val["url"]

    if data is None and isinstance(val, str):
        # Handle data URL: e.g. "data:image/png;base64,iVBOR..."
        if val.startswith("data:image/") and "base64," in val:
            try:
                parts = val.split("base64,", 1)
                if len(parts) == 2:
                    data = base64.b64decode(parts[1])
            except Exception:
                pass
        else:
            # Try as HTTP/HTTPS URL
            parsed = urllib.parse.urlparse(val)
            if parsed.scheme in ("http", "https"):
                with urllib.request.urlopen(val) as response:
                    data = response.read()
            else:
                # Try as local path first, if file exists
                try:
                    if Path(val).is_file():
                        with open(val, "rb") as f:
                            data = f.read()
                except Exception:
                    pass

                # If still no data, try to see if the string itself is raw base64 data
                if data is None:
                    try:
                        # Basic check: maybe it's raw base64
                        data = base64.b64decode(val)
                    except (binascii.Error, ValueError):
                        pass

    # If we couldn't resolve any data, fallback or return raw
    if data is None:
        return val

    # Convert data based on target_type
    target_lower = target_type.lower()
    if "pil.image" in target_lower or "image.image" in target_lower:
        if not is_pillow_available():
            raise ImportError("Pillow is required for this tool. Install smarter-mcp[multimodal].")
        import PIL.Image
        return PIL.Image.open(io.BytesIO(data)).copy()

    elif "numpy.ndarray" in target_lower or "ndarray" in target_lower:
        if not is_numpy_available() or not is_pillow_available():
            raise ImportError("Pillow and numpy are required for this tool. Install smarter-mcp[multimodal].")
        import numpy as np
        import PIL.Image
        img = PIL.Image.open(io.BytesIO(data))
        return np.array(img)

    return val

def coerce_to_fastmcp_image(val: Any) -> Image:
    """
    Converts a PIL Image, NumPy array, bytes, or Path into a fastmcp.Image.
    """
    if isinstance(val, Image):
        return val

    if isinstance(val, (Path, str)):
        return Image(path=str(val))

    if isinstance(val, bytes):
        return Image(data=val, format="png")

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

    return val
