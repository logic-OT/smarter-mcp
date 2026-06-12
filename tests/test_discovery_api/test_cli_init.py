"""Tests for the 'smarter-mcp init' single-file scan path.

Specifically exercises the fix for cli/main.py:365 where _resolve_implementations
returned a 3-tuple but the result was passed to merge_extraction as a dict,
causing AttributeError at runtime.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from smarter_mcp._decorators import clear_global_registry
from smarter_mcp.cli.main import cli


@pytest.fixture(autouse=True)
def _reset():
    clear_global_registry()
    yield
    clear_global_registry()


class TestInitSingleFile:
    def test_init_single_file_no_crash(self, tmp_path):
        """smarter-mcp init on a single .py file must not crash.

        Regression test for the 3-tuple unpack bug: _resolve_implementations
        returns (impls, failed, skipped) but was assigned to a plain variable
        and passed as a dict, causing AttributeError on .items()/.get().
        """
        src = tmp_path / "mytools.py"
        src.write_text(
            "def greet(name: str) -> str:\n"
            "    return f'Hello, {name}!'\n"
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        runner = CliRunner()
        result = runner.invoke(cli, ["init", str(src), "--output", str(output_dir)])

        assert result.exit_code == 0, (
            f"Expected exit_code=0 for 'smarter-mcp init <file.py>'; "
            f"got exit_code={result.exit_code}\nOutput:\n{result.output}"
            + (f"\nException: {result.exception}" if result.exception else "")
        )

    def test_init_single_file_lists_discovered_tool(self, tmp_path):
        """smarter-mcp init on a single .py file must write discovered tools into the manifest."""
        src = tmp_path / "tools.py"
        src.write_text(
            "def add(a: int, b: int) -> int:\n"
            "    return a + b\n"
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        runner = CliRunner()
        result = runner.invoke(cli, ["init", str(src), "--output", str(output_dir)])

        assert result.exit_code == 0, (
            f"exit_code={result.exit_code}\n{result.output}"
        )
        # Discovered tools are written as comments into the YAML manifest
        manifest = output_dir / "smarter-mcp.yaml"
        content = manifest.read_text()
        assert "add" in content, (
            f"Expected discovered tool 'add' in generated manifest; got:\n{content}"
        )

    def test_init_single_file_creates_manifest(self, tmp_path):
        """smarter-mcp init must write a smarter-mcp.yaml in the output directory."""
        src = tmp_path / "funcs.py"
        src.write_text("def noop() -> None:\n    pass\n")
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        runner = CliRunner()
        runner.invoke(cli, ["init", str(src), "--output", str(output_dir)])

        manifest = output_dir / "smarter-mcp.yaml"
        assert manifest.exists(), (
            f"Expected smarter-mcp.yaml to be created at {manifest}"
        )
