"""Helper module for dynamic import and target resolution of SmarterMCP servers."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import click

from smarter_mcp.config.manifest import find_manifest
from smarter_mcp.server.app import SmarterMCP


def detect_app(filepath: Path) -> SmarterMCP:
    """Import a Python file as a module and scan for a SmarterMCP instance.

    Looks for:
    1. Conventional names: 'app', 'server', 'mcp', 'smarter_mcp'
    2. Any module-level variable of type SmarterMCP.
    """
    filepath = Path(filepath).resolve()
    if not filepath.exists():
        raise click.ClickException(f"Target file not found: {filepath}")

    module_name = filepath.stem
    parent_dir = str(filepath.parent)

    # Ensure parent directory is in sys.path
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

    try:
        spec = importlib.util.spec_from_file_location(module_name, filepath)
        if spec is None or spec.loader is None:
            raise click.ClickException(f"Could not load module specification from file: {filepath}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception as e:
        raise click.ClickException(f"Failed to import Python file '{filepath}': {e}")

    # 1. Try conventional names first
    for name in ["app", "server", "mcp", "smarter_mcp"]:
        val = getattr(module, name, None)
        if val is not None and isinstance(val, SmarterMCP):
            return val

    # 2. Fall back to scanning all module-level variables
    candidates = []
    for key, val in module.__dict__.items():
        if not key.startswith("_") and isinstance(val, SmarterMCP):
            candidates.append((key, val))

    if len(candidates) == 1:
        return candidates[0][1]
    elif len(candidates) > 1:
        names = ", ".join(k for k, _ in candidates)
        raise click.ClickException(
            f"Multiple SmarterMCP instances found in '{filepath}' ({names}). "
            "Please name your target instance 'app', 'server', 'mcp', or 'smarter_mcp' to disambiguate."
        )

    raise click.ClickException(
        f"No SmarterMCP instance found in '{filepath}'. "
        "Ensure you instantiate `SmarterMCP()` at the module level in your script."
    )


def resolve_target(target: str | None, manifest: str | None) -> SmarterMCP:
    """Unified target resolution logic.

    Resolves:
    1. Explicit manifest file
    2. Target Python file (starts direct execution or CLI app scan)
    3. Target directory (source root scan)
    4. Implicit manifest in CWD/parents
    5. Implicit Python file in CWD
    6. Implicit directory scan of CWD
    """
    if manifest:
        manifest_path = Path(manifest).resolve()
        if not manifest_path.exists():
            raise click.ClickException(f"Manifest file not found: {manifest}")
        return SmarterMCP(manifest=manifest_path)

    if target:
        target_path = Path(target).resolve()
        if not target_path.exists():
            raise click.ClickException(f"Target path not found: {target}")

        if target_path.is_file():
            if target_path.suffix != ".py":
                raise click.ClickException(f"Target file must be a Python script (.py): {target}")
            return detect_app(target_path)
        elif target_path.is_dir():
            # Check if there is a manifest inside the target directory first
            found = find_manifest(target_path)
            if found:
                return SmarterMCP(manifest=found)
            return SmarterMCP(source_root=target_path)

    # Implicit lookup in current working directory
    cwd_manifest = find_manifest(".")
    if cwd_manifest:
        return SmarterMCP(manifest=cwd_manifest)

    for entrypoint in ["app.py", "server.py", "main.py"]:
        p = Path(entrypoint)
        if p.exists() and p.is_file():
            return detect_app(p)

    return SmarterMCP(source_root=".")
