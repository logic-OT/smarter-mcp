"""
Disk-backed extraction cache.

`build()` (and especially `--dev` hot-reload, which respawns a fresh subprocess
on every change) otherwise re-reads, re-parses, and re-enriches every file on
each start. This cache stores the *post-enrichment* `ExtractedModule` keyed by a
hash of the file's content, so an unchanged file is reloaded instead of
re-extracted.

Keying on content hash means edits invalidate automatically (the new content
hashes differently → miss → re-extract). Values are pickled because the IR tree
contains enums and the `MISSING` sentinel that JSON can't represent.

The cache is **opt-in** (off by default) so the test suite stays deterministic
and ordinary runs don't litter the working tree. Enable it explicitly, via the
``SMARTER_MCP_EXTRACTION_CACHE`` env var (set by ``serve --dev``), and disable it
entirely with ``SMARTER_MCP_NO_CACHE``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
from pathlib import Path

from .models import ExtractedModule

logger = logging.getLogger(__name__)

# Bump when the IR shape or extraction logic changes, to invalidate old entries.
CACHE_VERSION = 1

DEFAULT_CACHE_DIR = Path(".smarter-mcp") / "extraction-cache"

_ENABLE_ENV = "SMARTER_MCP_EXTRACTION_CACHE"
_DISABLE_ENV = "SMARTER_MCP_NO_CACHE"


def _truthy(value: str | None) -> bool:
    return bool(value) and value.lower() not in ("0", "false", "no", "")


def cache_enabled(explicit: bool) -> bool:
    """Resolve whether caching is active.

    On when explicitly requested OR when ``SMARTER_MCP_EXTRACTION_CACHE`` is set,
    unless ``SMARTER_MCP_NO_CACHE`` overrides (kill-switch wins).
    """
    if _truthy(os.environ.get(_DISABLE_ENV)):
        return False
    return explicit or _truthy(os.environ.get(_ENABLE_ENV))


class ExtractionCache:
    """Content-hash keyed, pickle-backed store of `ExtractedModule`s."""

    def __init__(self, cache_dir: str | Path | None = None):
        self.cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
        # Small in-process layer so repeated lookups in one run skip disk I/O.
        self._mem: dict[str, ExtractedModule] = {}

    def _key(self, source: str, module_name: str, use_inspect: bool) -> str:
        h = hashlib.sha256()
        h.update(source.encode("utf-8"))
        h.update(b"\x00")
        h.update(module_name.encode("utf-8"))
        h.update(b"\x00")
        h.update(b"inspect" if use_inspect else b"ast")
        h.update(b"\x00")
        h.update(str(CACHE_VERSION).encode("ascii"))
        return h.hexdigest()

    def _path_for(self, key: str) -> Path:
        return self.cache_dir / f"{key}.pkl"

    def get(self, source: str, module_name: str, use_inspect: bool) -> ExtractedModule | None:
        """Return the cached module for this source, or None on miss."""
        key = self._key(source, module_name, use_inspect)
        if key in self._mem:
            return self._mem[key]

        path = self._path_for(key)
        if not path.exists():
            return None
        try:
            module = pickle.loads(path.read_bytes())
        except Exception as e:  # noqa: BLE001 - corrupt/incompatible entry → miss
            logger.debug("Ignoring unreadable cache entry %s: %s", path, e)
            return None

        self._mem[key] = module
        return module

    def put(self, source: str, module_name: str, use_inspect: bool, module: ExtractedModule) -> None:
        """Store a module. Failures (unpicklable default, read-only FS) are non-fatal."""
        key = self._key(source, module_name, use_inspect)
        self._mem[key] = module
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path_for(key).with_suffix(".pkl.tmp")
            tmp.write_bytes(pickle.dumps(module))
            tmp.replace(self._path_for(key))  # atomic on POSIX
        except Exception as e:  # noqa: BLE001 - caching is best-effort
            logger.debug("Could not write cache entry for %s: %s", module_name, e)
