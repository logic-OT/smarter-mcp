"""
Disk-backed extraction cache.

``build()`` (and especially ``--dev`` hot-reload, which respawns a fresh
subprocess on every change) otherwise re-reads, re-parses, and re-enriches
every file on each start.  This cache stores the *post-enrichment*
``ExtractedModule`` keyed by a hash of the file's content, so an unchanged
file is reloaded instead of re-extracted.

Keying on content hash means edits invalidate automatically (the new content
hashes differently → miss → re-extract).

**Serialisation format: JSON, NOT pickle** (H5 security fix).

Pickle is a code-execution vector: anyone who can write a ``.pkl`` file under
the cache directory (shared CI runner, a poisoned repo shipping a pre-seeded
``.smarter-mcp/`` directory) achieves arbitrary code execution at next server
start.  The extraction IR is trivially JSON-encodable — enums are str subclasses,
``MISSING``/``NON_LITERAL`` sentinels map to marker strings, dataclasses map to
dicts.  An explicit encode/decode pair is safer and produces human-readable
cache entries.

The cache is **opt-in** (off by default) so the test suite stays deterministic
and ordinary runs don't litter the working tree.  Enable it explicitly via the
``SMARTER_MCP_EXTRACTION_CACHE`` env var (set by ``serve --dev``), and disable
it entirely with ``SMARTER_MCP_NO_CACHE``.

The cache directory is anchored to ``source_root`` (H5) to prevent loading a
cache that belongs to a different project when the server is started from a
different working directory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from .models import (
    _MISSING_TYPE,
    _NON_LITERAL_TYPE,
    MISSING,
    NON_LITERAL,
    CallableKind,
    ExtractedCallable,
    ExtractedClass,
    ExtractedModule,
    ExtractedParam,
    ParamKind,
)

logger = logging.getLogger(__name__)

# Bump when the IR shape or extraction logic changes, to invalidate old entries.
CACHE_VERSION = 2  # bumped from 1 (pickle→JSON migration)

# Relative to source_root when a source_root is supplied; absolute fallback.
_DEFAULT_CACHE_SUBDIR = Path(".smarter-mcp") / "extraction-cache"

_ENABLE_ENV = "SMARTER_MCP_EXTRACTION_CACHE"
_DISABLE_ENV = "SMARTER_MCP_NO_CACHE"

# Sentinel markers used in JSON serialisation
_MISSING_MARKER = "__SMARTER_MCP_MISSING__"
_NON_LITERAL_MARKER = "__SMARTER_MCP_NON_LITERAL__"


# ──────────────────────────────────────────────────────────────────────
# JSON encode / decode helpers
# ──────────────────────────────────────────────────────────────────────

def _encode_default(val: Any) -> Any:
    """Encode value for JSON serialisation."""
    if isinstance(val, _MISSING_TYPE):
        return _MISSING_MARKER
    if isinstance(val, _NON_LITERAL_TYPE):
        return _NON_LITERAL_MARKER
    raise TypeError(f"Object of type {type(val)} is not JSON serialisable")


def _encode_param(p: ExtractedParam) -> dict:
    default: Any
    if isinstance(p.default, _MISSING_TYPE):
        default = _MISSING_MARKER
    elif isinstance(p.default, _NON_LITERAL_TYPE):
        default = _NON_LITERAL_MARKER
    else:
        default = p.default
    return {
        "name": p.name,
        "annotation": p.annotation,
        "default": default,
        "kind": p.kind.value,
        "description": p.description,
        "inferred_type": p.inferred_type,
    }


def _decode_param(d: dict) -> ExtractedParam:
    raw_default = d["default"]
    if raw_default == _MISSING_MARKER:
        default = MISSING
    elif raw_default == _NON_LITERAL_MARKER:
        default = NON_LITERAL
    else:
        default = raw_default
    return ExtractedParam(
        name=d["name"],
        annotation=d.get("annotation"),
        default=default,
        kind=ParamKind(d["kind"]),
        description=d.get("description"),
        inferred_type=d.get("inferred_type"),
    )


def _encode_callable(c: ExtractedCallable) -> dict:
    return {
        "qualified_name": c.qualified_name,
        "kind": c.kind.value,
        "module_path": c.module_path,
        "class_name": c.class_name,
        "is_async": c.is_async,
        "parameters": [_encode_param(p) for p in c.parameters],
        "return_type": c.return_type,
        "docstring": c.docstring,
        "is_inherited": c.is_inherited,
        "has_variadic": c.has_variadic,
        "decorators": list(c.decorators),
        "source_lines": list(c.source_lines),
    }


def _decode_callable(d: dict) -> ExtractedCallable:
    return ExtractedCallable(
        qualified_name=d["qualified_name"],
        kind=CallableKind(d["kind"]),
        module_path=d["module_path"],
        class_name=d.get("class_name"),
        is_async=d.get("is_async", False),
        parameters=[_decode_param(p) for p in d.get("parameters", [])],
        return_type=d.get("return_type"),
        docstring=d.get("docstring"),
        is_inherited=d.get("is_inherited", False),
        has_variadic=d.get("has_variadic", False),
        decorators=d.get("decorators", []),
        source_lines=tuple(d.get("source_lines", [0, 0])),  # type: ignore[arg-type]
    )


def _encode_class(cls: ExtractedClass) -> dict:
    return {
        "name": cls.name,
        "qualified_name": cls.qualified_name,
        "module_path": cls.module_path,
        "docstring": cls.docstring,
        "bases": list(cls.bases),
        "methods": [_encode_callable(m) for m in cls.methods],
        "properties": [_encode_callable(p) for p in cls.properties],
        "init_params": [_encode_param(p) for p in cls.init_params],
        "decorators": list(cls.decorators),
        "source_lines": list(cls.source_lines),
    }


def _decode_class(d: dict) -> ExtractedClass:
    return ExtractedClass(
        name=d["name"],
        qualified_name=d["qualified_name"],
        module_path=d["module_path"],
        docstring=d.get("docstring"),
        bases=d.get("bases", []),
        methods=[_decode_callable(m) for m in d.get("methods", [])],
        properties=[_decode_callable(p) for p in d.get("properties", [])],
        init_params=[_decode_param(p) for p in d.get("init_params", [])],
        decorators=d.get("decorators", []),
        source_lines=tuple(d.get("source_lines", [0, 0])),  # type: ignore[arg-type]
    )


def _encode_module(mod: ExtractedModule) -> dict:
    return {
        "module_path": mod.module_path,
        "module_name": mod.module_name,
        "functions": [_encode_callable(f) for f in mod.functions],
        "classes": [_encode_class(c) for c in mod.classes],
        "docstring": mod.docstring,
        "all_exports": mod.all_exports,
    }


def _decode_module(d: dict) -> ExtractedModule:
    return ExtractedModule(
        module_path=d["module_path"],
        module_name=d["module_name"],
        functions=[_decode_callable(f) for f in d.get("functions", [])],
        classes=[_decode_class(c) for c in d.get("classes", [])],
        docstring=d.get("docstring"),
        all_exports=d.get("all_exports"),
    )


# ──────────────────────────────────────────────────────────────────────
# Cache helpers
# ──────────────────────────────────────────────────────────────────────

def _truthy(value: str | None) -> bool:
    return bool(value) and value.lower() not in ("0", "false", "no", "")


def cache_enabled(explicit: bool) -> bool:
    """Resolve whether caching is active.

    On when explicitly requested OR when ``SMARTER_MCP_EXTRACTION_CACHE`` is
    set, unless ``SMARTER_MCP_NO_CACHE`` overrides (kill-switch wins).
    """
    if _truthy(os.environ.get(_DISABLE_ENV)):
        return False
    return explicit or _truthy(os.environ.get(_ENABLE_ENV))


class ExtractionCache:
    """Content-hash keyed, JSON-backed store of ``ExtractedModule``\\s.

    H5: replaced pickle serialisation with explicit JSON encode/decode so that
    a malicious or corrupt cache file cannot achieve code execution.  The cache
    directory is anchored to ``source_root`` (not bare CWD) so starting the
    server from a different directory never loads a stale/foreign cache.
    """

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        source_root: str | Path | None = None,
    ):
        if cache_dir is not None:
            self.cache_dir = Path(cache_dir)
        elif source_root is not None:
            # H5: anchor to source_root so CWD changes don't load the wrong cache.
            self.cache_dir = Path(source_root) / _DEFAULT_CACHE_SUBDIR
        else:
            self.cache_dir = _DEFAULT_CACHE_SUBDIR

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
        # .json extension (not .pkl) — H5: no pickle anywhere in the cache.
        return self.cache_dir / f"{key}.json"

    def get(
        self, source: str, module_name: str, use_inspect: bool
    ) -> ExtractedModule | None:
        """Return the cached module for this source, or None on miss."""
        key = self._key(source, module_name, use_inspect)
        if key in self._mem:
            return self._mem[key]

        path = self._path_for(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            module = _decode_module(data)
        except Exception as e:
            logger.debug("Ignoring unreadable cache entry %s: %s", path, e)
            return None

        self._mem[key] = module
        return module

    def put(
        self,
        source: str,
        module_name: str,
        use_inspect: bool,
        module: ExtractedModule,
    ) -> None:
        """Store a module. Failures (unserializable value, read-only FS) are
        non-fatal — caching is always best-effort."""
        key = self._key(source, module_name, use_inspect)
        self._mem[key] = module
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path_for(key).with_suffix(".json.tmp")
            data = json.dumps(_encode_module(module), default=_encode_default)
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(self._path_for(key))  # atomic on POSIX
        except Exception as e:
            logger.debug("Could not write cache entry for %s: %s", module_name, e)
