"""Tests for H19 — dependency hygiene."""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_h19_llm_import_error_message_references_extra():
    """When openai is not installed, the actual error message must say 'smarter-mcp[llm]'."""
    import smarter_mcp.llm.client as client_mod
    from smarter_mcp.config.manifest import LLMConfig
    from smarter_mcp.llm.client import LLMNotAvailableError

    config = LLMConfig(enabled=True, provider="openai", api_key_env="TEST_FAKE_KEY")

    # Simulate missing openai by setting sys.modules["openai"] = None.
    # Python's import machinery raises ImportError when a module entry is None,
    # so the `from openai import OpenAI` inside OpenAIClient.__init__ will fail
    # regardless of whether openai is installed in the test environment.
    saved = sys.modules.get("openai")
    sys.modules["openai"] = None  # type: ignore[assignment]
    try:
        client_mod.OpenAIClient(config)
        raise AssertionError("Expected LLMNotAvailableError to be raised")
    except LLMNotAvailableError as e:
        assert "smarter-mcp[llm]" in str(e), (
            f"Error message must reference 'smarter-mcp[llm]', got: {e!r}"
        )
    finally:
        if saved is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = saved


def test_h19_llm_client_error_message_text():
    """The LLMNotAvailableError message in client.py must contain 'smarter-mcp[llm]'."""
    client_src = _REPO_ROOT / "src/smarter_mcp/llm/client.py"
    content = client_src.read_text()
    assert "smarter-mcp[llm]" in content, (
        "client.py error message must say 'pip install smarter-mcp[llm]', "
        "not 'pip install smarter-mcp'. Current content around ImportError:\n"
        + "\n".join(
            line for line in content.splitlines()
            if "Install" in line or "smarter-mcp" in line
        )
    )


def test_h19_structlog_not_in_source():
    """structlog must not be imported anywhere in smarter_mcp src."""
    src_root = _REPO_ROOT / "src/smarter_mcp"
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
    src_root = _REPO_ROOT / "src/smarter_mcp"
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
    pyproject = _REPO_ROOT / "pyproject.toml"
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
    pyproject = _REPO_ROOT / "pyproject.toml"
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
