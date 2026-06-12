"""Tests for C5 — extraction errors surfaced in discover(), validate CLI, extraction_result."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from smarter_mcp import SmarterMCP
from smarter_mcp._decorators import clear_global_registry
from smarter_mcp.extractor.models import ExtractionResult


@pytest.fixture(autouse=True)
def _reset():
    clear_global_registry()
    yield
    clear_global_registry()


class TestExtractionResultPopulated:
    def test_extraction_result_is_set_after_discover(self):
        """extraction_result must be populated after discover()."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "ok.py").write_text("def fine(x: int) -> int:\n    return x\n")
            app = SmarterMCP(name="er-test", use_inspect=False)
            app.discover(td)
            er = app.extraction_result
            assert er is not None, "extraction_result must not be None after discover()"
            assert isinstance(er, ExtractionResult)

    def test_extraction_result_accumulates_across_discovers(self, tmp_path):
        """Calling discover() twice must accumulate errors in extraction_result."""
        dir1 = tmp_path / "dir1"
        dir1.mkdir()
        (dir1 / "a.py").write_text("def alpha(x: int) -> int:\n    return x\n")

        dir2 = tmp_path / "dir2"
        dir2.mkdir()
        (dir2 / "broken.py").write_text("def oops(:\n    return 1\n")

        app = SmarterMCP(name="multi-discover", use_inspect=False)
        app.discover(str(dir1))
        app.discover(str(dir2))
        er = app.extraction_result
        assert er is not None
        assert any("broken" in e or "syntax" in e.lower() for e in er.errors), (
            f"Expected a syntax-error entry in extraction_result.errors; got {er.errors!r}"
        )


class TestErrorsLogged:
    def test_syntax_error_logged_at_error_level(self, tmp_path, caplog):
        """Syntax errors in source files must be logged at ERROR level."""
        import logging

        (tmp_path / "broken.py").write_text("def oops(:\n    return 1\n")
        (tmp_path / "ok.py").write_text("def fine(x: int) -> int:\n    return x\n")

        app = SmarterMCP(name="log-test", use_inspect=False)
        with caplog.at_level(logging.ERROR, logger="smarter_mcp.server.app"):
            app.discover(str(tmp_path))

        error_messages = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("broken" in m or "syntax" in m.lower() or "error" in m.lower()
                   for m in error_messages), (
            f"Expected an ERROR-level log about the broken file; got: {error_messages!r}"
        )


class TestValidateCLI:
    def test_validate_exits_nonzero_on_syntax_error(self, tmp_path):
        """validate CLI must exit non-zero when extraction produced errors."""
        from click.testing import CliRunner

        from smarter_mcp.cli.main import cli

        (tmp_path / "broken.py").write_text("def oops(:\n    return 1\n")
        runner = CliRunner()
        result = runner.invoke(cli, ["validate", str(tmp_path)])
        assert result.exit_code != 0, (
            f"Expected non-zero exit from validate with syntax errors; "
            f"got exit_code={result.exit_code}\nOutput:\n{result.output}"
        )
        assert "ERROR" in result.output or "error" in result.output.lower(), (
            f"Expected ERROR in output; got:\n{result.output}"
        )
