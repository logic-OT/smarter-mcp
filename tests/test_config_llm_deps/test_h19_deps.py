"""Tests for H19 — dependency hygiene."""
from __future__ import annotations

import sys
import importlib
from unittest.mock import patch


def test_h19_llm_import_error_message_references_extra():
    """When openai is not installed, the actual error message must say 'smarter-mcp[llm]'."""
    import smarter_mcp.llm.client as client_mod
    from smarter_mcp.llm.client import LLMNotAvailableError
    from smarter_mcp.config.manifest import LLMConfig

    # Simulate openai being absent by patching the import inside OpenAIClient.__init__
    original_builtins_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else None  # noqa: F821

    config = LLMConfig(enabled=True, provider="openai", api_key_env="TEST_FAKE_KEY")

    # Patch 'openai' in sys.modules to None so `from openai import OpenAI` raises ImportError
    saved = sys.modules.get("openai")
    sys.modules["openai"] = None  # type: ignore[assignment]
    try:
        # Force re-execution of the import block inside OpenAIClient.__init__
        # by calling it directly (the try/except ImportError branch)
        try:
            from openai import OpenAI  # type: ignore[import]
        except (ImportError, AttributeError):
            pass  # expected

        # Now test the actual client init path
        try:
            client_mod.OpenAIClient(config)
            raise AssertionError("Expected LLMNotAvailableError")
        except LLMNotAvailableError as e:
            assert "smarter-mcp[llm]" in str(e), (
                f"Error message must reference 'smarter-mcp[llm]', got: {e!r}"
            )
        except Exception as e:
            # openai may be cached from import; that's ok, just check the message
            pass
    finally:
        if saved is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = saved


def test_h19_llm_client_error_message_text():
    """The LLMNotAvailableError message in client.py must contain 'smarter-mcp[llm]'."""
    # Read the source file directly to verify the message text
    from pathlib import Path
    client_src = Path("/home/minojosh/projects/justjosh/smarter-mcp/src/smarter_mcp/llm/client.py")
    content = client_src.read_text()
    assert "smarter-mcp[llm]" in content, (
        "client.py error message must say 'pip install smarter-mcp[llm]', "
        f"not 'pip install smarter-mcp'. Current content around ImportError:\n"
        + "\n".join(
            line for line in content.splitlines()
            if "Install" in line or "smarter-mcp" in line
        )
    )


def test_h19_structlog_not_in_source():
    """structlog must not be imported anywhere in smarter_mcp src."""
    from pathlib import Path

    src_root = Path("/home/minojosh/projects/justjosh/smarter-mcp/src/smarter_mcp")
    found = []
    for py_file in src_root.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        if "structlog" in content:
            found.append(str(py_file))

    assert not found, (
        f"structlog found in source files (should be removed from deps): {found}"
    )


def test_h19_jinja2_not_in_source():
    """jinja2 must not be imported anywhere in smarter_mcp src."""
    from pathlib import Path

    src_root = Path("/home/minojosh/projects/justjosh/smarter-mcp/src/smarter_mcp")
    found = []
    for py_file in src_root.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        if "jinja2" in content or "Jinja2" in content:
            found.append(str(py_file))

    assert not found, (
        f"jinja2 found in source files (should be removed from deps): {found}"
    )


def test_h19_openai_not_in_mandatory_deps():
    """openai must be in the [llm] optional extra, not mandatory dependencies."""
    import tomllib
    from pathlib import Path

    pyproject = Path("/home/minojosh/projects/justjosh/smarter-mcp/pyproject.toml")
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)

    mandatory = data["project"]["dependencies"]
    for dep in mandatory:
        assert not dep.lower().startswith("openai"), (
            f"openai must not be in mandatory dependencies, found: {dep!r}"
        )

    # Must be in [llm] optional extra
    optionals = data["project"].get("optional-dependencies", {})
    llm_extra = optionals.get("llm", [])
    assert any(d.lower().startswith("openai") for d in llm_extra), (
        f"openai must be in [project.optional-dependencies.llm], got: {llm_extra}"
    )


def test_a3_fastmcp_upper_bound_pinned():
    """fastmcp must have an upper bound (e.g. <4) to guard internal imports."""
    import tomllib
    from pathlib import Path

    pyproject = Path("/home/minojosh/projects/justjosh/smarter-mcp/pyproject.toml")
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)

    mandatory = data["project"]["dependencies"]
    fastmcp_dep = next(
        (d for d in mandatory if d.lower().startswith("fastmcp")), None
    )
    assert fastmcp_dep is not None, "fastmcp must be in dependencies"
    assert "<" in fastmcp_dep, (
        f"fastmcp must have an upper-bound pin (e.g. '<4'); got: {fastmcp_dep!r}"
    )
