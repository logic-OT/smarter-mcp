"""Tests for C3 — positional name + source_root validation + bounded find_manifest."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from smarter_mcp import SmarterMCP
from smarter_mcp._decorators import clear_global_registry


@pytest.fixture(autouse=True)
def _reset():
    clear_global_registry()
    yield
    clear_global_registry()


class TestPositionalName:
    def test_positional_name_sets_config_name(self):
        """SmarterMCP('my-server') must set config.name, not treat it as a path."""
        with tempfile.TemporaryDirectory() as td:
            prev = os.getcwd()
            os.chdir(td)
            try:
                app = SmarterMCP("my-server")
                assert app.config.name == "my-server", (
                    f"Expected config.name='my-server', got {app.config.name!r}"
                )
                # No phantom source entries created from the name
                assert app.config.sources == [], (
                    f"Expected no sources, got {app.config.sources!r}"
                )
            finally:
                os.chdir(prev)

    def test_keyword_name_still_works(self):
        """Existing keyword-style SmarterMCP(name=...) must still work."""
        app = SmarterMCP(name="kw-server")
        assert app.config.name == "kw-server"

    def test_name_overrides_manifest_name(self):
        """Positional name overrides the manifest's name field."""
        with tempfile.TemporaryDirectory() as td:
            mf = Path(td) / "smarter-mcp.yaml"
            mf.write_text("name: manifest-name\n")
            app = SmarterMCP("override-name", manifest=str(mf))
            assert app.config.name == "override-name"


class TestSourceRootValidation:
    def test_nonexistent_source_root_raises(self):
        """An explicitly supplied source_root that doesn't exist must raise ValueError."""
        with pytest.raises(ValueError, match="source_root does not exist"):
            SmarterMCP(source_root="/does/not/exist/ever")

    def test_existing_source_root_does_not_raise(self):
        """An existing source_root must not raise."""
        with tempfile.TemporaryDirectory() as td:
            app = SmarterMCP(source_root=td)
            assert app is not None

    def test_discover_nonexistent_path_raises(self):
        """discover() on a nonexistent path must raise ValueError."""
        app = SmarterMCP(name="test")
        with pytest.raises(ValueError, match="source_root does not exist"):
            app.discover("/does/not/exist/ever")


class TestBoundedFindManifest:
    def test_find_manifest_stops_at_git_boundary(self):
        """find_manifest must not climb past a .git directory.

        Layout:
          grandparent/               ← stray manifest lives here
            smarter-mcp.yaml
            parent/                  ← .git boundary here, NO manifest
              .git/
              subproject/            ← search starts here, NO manifest

        Searching from subproject reaches parent, sees .git and stops.
        The manifest in grandparent must never be returned.
        """
        from smarter_mcp.config.manifest import find_manifest

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)

            # Grandparent: stray manifest (should NOT be found)
            grandparent = td_path / "grandparent"
            grandparent.mkdir()
            (grandparent / "smarter-mcp.yaml").write_text("name: stray-parent\n")

            # Parent: VCS boundary, no manifest
            parent = grandparent / "parent"
            parent.mkdir()
            (parent / ".git").mkdir()

            # Search start: no manifest here either
            subproject = parent / "subproject"
            subproject.mkdir()

            result = find_manifest(subproject)
            assert result is None, (
                f"Expected None (stopped at .git boundary in parent/), got {result!r}"
            )

    def test_find_manifest_finds_manifest_at_git_root(self):
        """Manifest in the same dir as .git should still be found."""
        from smarter_mcp.config.manifest import find_manifest

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / ".git").mkdir()
            mf = td_path / "smarter-mcp.yaml"
            mf.write_text("name: my-project\n")

            result = find_manifest(td_path)
            assert result == mf, f"Expected {mf}, got {result!r}"
