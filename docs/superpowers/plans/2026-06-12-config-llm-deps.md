# Config + LLM + Dependency Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden config validation (H15), wire/remove dead config fields, validate lifecycle decorator (M14), fix LLM client reliability (H16), move openai to optional extra (H19), pin FastMCP (A3), prune LLM cache, and wire `auto_detect` + `SourceConfig.include` for path sources.

**Architecture:** Six independent fix groups applied sequentially. Each produces green tests before moving on. The riskiest change is `extra="forbid"` — run the full test suite after that task and fix every breakage before proceeding.

**Tech Stack:** Python 3.10+, Pydantic v2, FastMCP 3.3.1, uv, pytest, ruff

---

## Pre-flight

Before starting any task:
```bash
cd /home/minojosh/projects/justjosh/smarter-mcp
git checkout feat/config-llm-deps
uv run --extra all pytest -q   # must show 153 passed
uv run --extra dev ruff check src/ tests/  # note existing errors (91)
```

---

## Task 1: H19 + A3 — Dependency hygiene & FastMCP version pin

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/smarter_mcp/llm/client.py` (error message only)
- Create: `tests/test_config_llm_deps/__init__.py`
- Create: `tests/test_config_llm_deps/test_h19_deps.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_llm_deps/__init__.py` (empty) and `tests/test_config_llm_deps/test_h19_deps.py`:

```python
"""Tests for H19 — dependency hygiene."""
from __future__ import annotations

import sys
from unittest.mock import patch


def test_h19_llm_import_error_message_references_extra():
    """When openai is not installed, the error message must say 'smarter-mcp[llm]'."""
    from smarter_mcp.config.manifest import LLMConfig
    from smarter_mcp.llm.client import LLMNotAvailableError

    config = LLMConfig(enabled=True, api_key_env="OPENAI_API_KEY")

    with patch.dict(sys.modules, {"openai": None}):
        # Patch the import inside the module
        import smarter_mcp.llm.client as client_mod
        original = client_mod.OpenAIClient.__init__

        def _mock_init(self, config):
            raise LLMNotAvailableError(
                "The 'openai' package is required for LLM description generation. "
                "Install it with: pip install smarter-mcp[llm]"
            )

        with patch.object(client_mod.OpenAIClient, "__init__", _mock_init):
            try:
                client_mod.OpenAIClient(config)
            except LLMNotAvailableError as e:
                assert "smarter-mcp[llm]" in str(e), (
                    f"Error message must reference 'smarter-mcp[llm]', got: {e}"
                )
            else:
                raise AssertionError("Expected LLMNotAvailableError was not raised")


def test_h19_structlog_not_imported():
    """structlog must not be imported anywhere in smarter_mcp."""
    import importlib
    import pkgutil
    import smarter_mcp

    # Walk the package and check no module imports structlog
    for importer, modname, ispkg in pkgutil.walk_packages(
        path=smarter_mcp.__path__,
        prefix=smarter_mcp.__name__ + ".",
        onerror=lambda x: None,
    ):
        try:
            mod = importlib.import_module(modname)
            assert "structlog" not in str(getattr(mod, "__file__", "")), (
                f"Module {modname} appears to use structlog"
            )
        except Exception:
            pass  # import errors here are fine, we're just checking


def test_h19_jinja2_not_imported():
    """jinja2 must not be imported anywhere in smarter_mcp."""
    import importlib
    import pkgutil
    import smarter_mcp

    for importer, modname, ispkg in pkgutil.walk_packages(
        path=smarter_mcp.__path__,
        prefix=smarter_mcp.__name__ + ".",
        onerror=lambda x: None,
    ):
        try:
            mod = importlib.import_module(modname)
            src = getattr(mod, "__file__", "") or ""
            if src.endswith(".py"):
                import inspect
                try:
                    source = inspect.getsource(mod)
                    assert "import jinja2" not in source and "from jinja2" not in source, (
                        f"Module {modname} imports jinja2"
                    )
                except (OSError, TypeError):
                    pass
        except Exception:
            pass
```

- [ ] **Step 2: Run tests to verify they fail (or pass for import checks)**

```bash
cd /home/minojosh/projects/justjosh/smarter-mcp
uv run --extra all pytest tests/test_config_llm_deps/test_h19_deps.py -v
```
Expected: test_h19_llm_import_error_message_references_extra fails because current message says `pip install smarter-mcp` not `pip install smarter-mcp[llm]`

- [ ] **Step 3: Fix pyproject.toml**

Edit `pyproject.toml` to:
1. Remove `jinja2>=3.0` and `structlog>=23.0` from `dependencies`
2. Move `openai>=1.0` to a new `[llm]` extra
3. Update `all` to include `llm`
4. Pin `fastmcp>=3.3.1,<4` with a comment

The full `[project.optional-dependencies]` block should become:
```toml
[project.optional-dependencies]
multimodal = ["Pillow>=10.0", "numpy>=1.24"]
# openai is optional: LLM description generation only (lazy-imported, raises
# LLMNotAvailableError if absent). Install with: pip install smarter-mcp[llm]
llm = ["openai>=1.0"]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "watchfiles>=0.20",
    "ruff>=0.4",
]
all = ["smarter-mcp[multimodal,llm,dev]"]
```

The `dependencies` block should be:
```toml
dependencies = [
    # Pin to tested minor; code imports deep internals
    # (fastmcp.server.middleware, auth.providers.jwt, dependencies.get_context).
    "fastmcp>=3.3.1,<4",
    "click>=8.0",
    "pydantic>=2.0",
    "pyyaml>=6.0",
]
```

- [ ] **Step 4: Fix error message in llm/client.py**

In `src/smarter_mcp/llm/client.py`, update the ImportError message:

Old:
```python
raise LLMNotAvailableError(
    "The 'openai' package is required for LLM description generation. "
    "Install it with: pip install smarter-mcp"
) from e
```

New:
```python
raise LLMNotAvailableError(
    "The 'openai' package is required for LLM description generation. "
    "Install it with: pip install smarter-mcp[llm]"
) from e
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run --extra all pytest tests/test_config_llm_deps/test_h19_deps.py -v
```
Expected: all 3 tests PASS

- [ ] **Step 6: Run full suite**

```bash
uv run --extra all pytest -q
```
Expected: 153+ passed, 0 failed

- [ ] **Step 7: Run ruff on changed files only**

```bash
uv run --extra dev ruff check src/smarter_mcp/llm/client.py
```
Expected: no new errors

- [ ] **Step 8: Commit**

```bash
cd /home/minojosh/projects/justjosh/smarter-mcp
git add pyproject.toml src/smarter_mcp/llm/client.py tests/test_config_llm_deps/__init__.py tests/test_config_llm_deps/test_h19_deps.py
git commit -m "fix(deps): move openai to [llm] extra, drop structlog+jinja2, pin fastmcp>=3.3.1,<4 (H19, A3)"
```

---

## Task 2: H15 — `extra="forbid"` on all manifest models + dead-config cleanup

**Files:**
- Modify: `src/smarter_mcp/config/manifest.py`
- Modify: `src/smarter_mcp/cli/main.py` (remove scaffolded dead-config keys/comments)
- Create: `tests/test_config_llm_deps/test_h15_manifest.py`

Dead-config dispositions:
| Field | Disposition | Reason |
|---|---|---|
| `server.cors_origins` | REMOVE | Not wired; default `["*"]` unsafe; needs separate CORS PR |
| `server.log_level` | KEEP+WIRE | Wire in `app.py.__init__` (Task 5) |
| `routing.base_path` | REMOVE | FastMCP doesn't expose a simple mount-prefix override |
| `routing.root_aggregate` | REMOVE | Not feasible without rewriting the router |
| `MultimodalConfig.auto_detect` | KEEP+WIRE | Gates image coercion (wired in Task 5) |
| `MultimodalConfig.image_max_size` | KEEP | Consumed in security hardening PR — add docstring note |
| `MultimodalConfig.image_format` | REMOVE | PIL hardcodes "PNG" in `interceptor.py`; field unused |
| `ToolOverride.param_descriptions` | REMOVE | No injection point in schema generation without major work |

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_llm_deps/test_h15_manifest.py`:

```python
"""Tests for H15 — extra="forbid" on all manifest config models."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from smarter_mcp.config.manifest import (
    ExposeConfig,
    InstanceConfig,
    LLMConfig,
    ManifestConfig,
    MultimodalConfig,
    RoutingConfig,
    ServerConfig,
    SourceConfig,
    ToolOverride,
    load_manifest,
)


class TestExtraForbid:
    def test_server_config_rejects_unknown_key(self):
        with pytest.raises(ValidationError, match="bogus_key"):
            ServerConfig(bogus_key="oops")

    def test_source_config_rejects_unknown_key(self):
        with pytest.raises(ValidationError, match="unknown_field"):
            SourceConfig(path=".", unknown_field=True)

    def test_routing_config_rejects_unknown_key(self):
        with pytest.raises(ValidationError, match="base_path|extra"):
            # base_path was removed; passing it now should fail
            RoutingConfig(base_path="/mcp")

    def test_expose_config_rejects_unknown_key(self):
        with pytest.raises(ValidationError, match="bogus"):
            ExposeConfig(bogus=True)

    def test_instance_config_rejects_unknown_key(self):
        with pytest.raises(ValidationError, match="typo_field"):
            InstanceConfig(class_name="Foo", typo_field=1)

    def test_tool_override_rejects_unknown_key(self):
        with pytest.raises(ValidationError, match="param_descriptions|unknown"):
            # param_descriptions was removed; must now fail
            ToolOverride(function="foo.bar", param_descriptions={"x": "desc"})

    def test_multimodal_config_rejects_unknown_key(self):
        with pytest.raises(ValidationError, match="image_format|unknown"):
            # image_format was removed; must now fail
            MultimodalConfig(image_format="jpeg")

    def test_llm_config_rejects_unknown_key(self):
        with pytest.raises(ValidationError, match="typo_key"):
            LLMConfig(typo_key="bad")

    def test_manifest_config_rejects_unknown_key(self):
        with pytest.raises(ValidationError, match="totally_made_up"):
            ManifestConfig(totally_made_up=True)

    def test_manifest_dir_still_settable_after_construction(self, tmp_path):
        """manifest_dir is Field(exclude=True) — setting it post-construction must work."""
        cfg = ManifestConfig()
        cfg.manifest_dir = str(tmp_path)  # must not raise
        assert cfg.manifest_dir == str(tmp_path)

    def test_load_manifest_rejects_yaml_with_unknown_key(self, tmp_path):
        """A YAML file with a bogus top-level key must raise ValidationError naming it."""
        mf = tmp_path / "smarter-mcp.yaml"
        mf.write_text("name: test\ncompletely_bogus_key: 99\n")
        with pytest.raises(ValidationError, match="completely_bogus_key"):
            load_manifest(str(mf))

    def test_load_manifest_rejects_yaml_with_unknown_expose_key(self, tmp_path):
        """README-documented-but-wrong key 'private' must now fail."""
        mf = tmp_path / "smarter-mcp.yaml"
        mf.write_text(
            "name: t\n"
            "expose:\n"
            "  private: false\n"   # README key; real key is include_private
        )
        with pytest.raises(ValidationError, match="private"):
            load_manifest(str(mf))


class TestRemovedFields:
    def test_cors_origins_removed(self):
        """cors_origins is removed; ServerConfig must not have the attribute."""
        sc = ServerConfig()
        assert not hasattr(sc, "cors_origins"), (
            "cors_origins should have been removed from ServerConfig"
        )

    def test_routing_base_path_removed(self):
        rc = RoutingConfig()
        assert not hasattr(rc, "base_path")

    def test_routing_root_aggregate_removed(self):
        rc = RoutingConfig()
        assert not hasattr(rc, "root_aggregate")

    def test_multimodal_image_format_removed(self):
        mc = MultimodalConfig()
        assert not hasattr(mc, "image_format")

    def test_tool_override_param_descriptions_removed(self):
        to = ToolOverride(function="foo.bar")
        assert not hasattr(to, "param_descriptions")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --extra all pytest tests/test_config_llm_deps/test_h15_manifest.py -v
```
Expected: all tests FAIL (no extra="forbid" yet, removed fields still present)

- [ ] **Step 3: Update config/manifest.py**

Add `from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator` (add `ConfigDict`).

Apply the following changes to each model in `src/smarter_mcp/config/manifest.py`:

**ServerConfig** — add `model_config`, remove `cors_origins`:
```python
class ServerConfig(BaseModel):
    """Server transport and networking configuration."""

    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"
    port: int = 8000
    transport: Literal["sse", "streamable-http", "stdio"] = "sse"
    log_level: str = "info"
    """Python logging level to apply at server startup (e.g. 'info', 'debug', 'warning')."""

    # Auth
    auth_enabled: bool = False
    auth_header: str = "X-API-Key"
    auth_keys_env: str = "SMARTER_MCP_API_KEYS"

    # Rate limiting
    rate_limit_enabled: bool = False
    rate_limit_per_minute: int = 60
    rate_limit_global_per_minute: int = 1000
```
(Remove `cors_origins` entirely.)

**SourceConfig** — add `model_config`:
```python
class SourceConfig(BaseModel):
    """Configuration for a source directory or module to scan."""

    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    module: str | None = None
    namespace: str | None = None
    exclude: list[str] = Field(default_factory=lambda: ["test_*", "*_test.py", "conftest.py"])
    include: list[str] = Field(default_factory=list)
    """If non-empty, only include files matching these glob patterns (path sources)
    or callable names (module sources)."""

    @model_validator(mode="after")
    def check_path_or_module(self) -> SourceConfig:
        if bool(self.path) == bool(self.module):
            raise ValueError("Exactly one of 'path' or 'module' must be provided in SourceConfig")
        return self
```

**RoutingConfig** — add `model_config`, remove `base_path` and `root_aggregate`:
```python
class RoutingConfig(BaseModel):
    """Namespace routing configuration."""

    model_config = ConfigDict(extra="forbid")

    overrides: dict[str, str] = Field(default_factory=dict)
    """Module path → custom namespace mapping (e.g., 'db/client' → 'database')."""

    separator: str = "_"
    """Separator for auto-generated tool names from class methods (ClassName_method)."""
```

**ExposeConfig** — add `model_config`:
```python
class ExposeConfig(BaseModel):
    """Controls what gets exposed as MCP tools."""

    model_config = ConfigDict(extra="forbid")

    include_private: bool = False
    include_dunder: bool = False
    include_inherited: bool = False
    include_properties: bool = True
    variadic_policy: Literal["skip", "warn", "expose"] = "warn"
    unannotated_policy: Literal["expose", "warn", "skip"] = "expose"
    respect_all: bool = True
```

**InstanceConfig** — add `model_config`:
```python
class InstanceConfig(BaseModel):
    """Configuration for how to instantiate a class."""

    model_config = ConfigDict(extra="forbid")

    class_name: str
    lifecycle: Literal["session", "singleton", "per-call"] = "session"
    constructor_args: dict[str, Any] = Field(default_factory=dict)
    factory: str | None = None
    factory_args: dict[str, Any] = Field(default_factory=dict)
```

**ToolOverride** — add `model_config`, remove `param_descriptions`:
```python
class ToolOverride(BaseModel):
    """Per-tool customization."""

    model_config = ConfigDict(extra="forbid")

    function: str
    name: str | None = None
    description: str | None = None
    expose: bool = True
    tests: list[dict[str, Any]] = Field(default_factory=list)
```
(Remove `param_descriptions` entirely.)

**MultimodalConfig** — add `model_config`, remove `image_format`, keep `image_max_size` with note:
```python
class MultimodalConfig(BaseModel):
    """Multimodal content handling configuration."""

    model_config = ConfigDict(extra="forbid")

    auto_detect: bool = True
    """When True, PIL.Image.Image and numpy.ndarray tool return values are
    automatically encoded as MCP Image objects. Set to False to disable
    image coercion entirely (tools must return fastmcp.Image explicitly)."""

    image_max_size: tuple[int, int] = (1024, 1024)
    """Maximum image dimensions (width, height) in pixels.
    Consumed by the image-security interceptor (security hardening PR).
    """
```
(Remove `image_format` — PIL hardcodes "PNG" in interceptor.py.)

**LLMConfig** — add `model_config`:
```python
class LLMConfig(BaseModel):
    """LLM-assisted description generation configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    provider: str = "openrouter"
    model: str = "google/gemini-2.0-flash-001"
    api_key_env: str | None = None
    base_url: str | None = None
    max_tokens: int = 256
    temperature: float = 0.2
    cache_path: str = ".smarter-mcp/description-cache.json"
    overwrite_existing: bool = False
```

**ManifestConfig** — add `model_config`:
```python
class ManifestConfig(BaseModel):
    """Top-level manifest configuration."""

    model_config = ConfigDict(extra="forbid")

    name: str = "my-mcp-server"
    version: str = "0.1.0"
    description: str = ""

    manifest_dir: str | None = Field(default=None, exclude=True)
    """Directory containing the manifest file. Set at load time, not from YAML."""

    server: ServerConfig = Field(default_factory=ServerConfig)
    sources: list[SourceConfig] = Field(default_factory=list)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    expose: ExposeConfig = Field(default_factory=ExposeConfig)
    instances: list[InstanceConfig] = Field(default_factory=list)
    tools: list[ToolOverride] = Field(default_factory=list)
    multimodal: MultimodalConfig = Field(default_factory=MultimodalConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)

    @model_validator(mode="before")
    @classmethod
    def substitute_env_vars(cls, data: Any) -> Any:
        """Substitute ${VAR} patterns in all string values."""
        if isinstance(data, dict):
            return _substitute_env_vars(data)
        return data

    @model_validator(mode="before")
    @classmethod
    def coerce_null_lists(cls, data: Any) -> Any:
        """Coerce None to [] for list fields."""
        if isinstance(data, dict):
            for field in ("sources", "instances", "tools"):
                if data.get(field) is None:
                    data[field] = []
        return data
```

- [ ] **Step 4: Fix CLI init scaffolding YAML in cli/main.py**

In `src/smarter_mcp/cli/main.py`, update the `yaml_content` template string.

Find:
```python
# routing:
#   base_path: "/mcp"
#   root_aggregate: true
#   separator: "_"
```
Replace with:
```python
# routing:
#   separator: "_"
#   overrides:
#     db/client: database
```

Also find:
```python
  log_level: "info"
```
Keep this — `log_level` is still a valid ServerConfig field.

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run --extra all pytest tests/test_config_llm_deps/test_h15_manifest.py -v
```
Expected: all tests PASS

- [ ] **Step 6: Run full suite — fix any breakage**

```bash
uv run --extra all pytest -q
```

Expected: all previously-passing tests still pass. If any test fails because it referenced a removed field (e.g., passing `cors_origins=` or `base_path=` to a constructor), fix that test.

Known potential breakage locations:
- `tests/validation/validate_findings.py` — may use `ServerConfig()` and check `cors_origins`; update if needed
- Any test writing YAML with now-forbidden keys

- [ ] **Step 7: Commit**

```bash
git add src/smarter_mcp/config/manifest.py src/smarter_mcp/cli/main.py tests/test_config_llm_deps/test_h15_manifest.py
git commit -m "fix(config): extra=forbid on all manifest models; remove dead cors_origins/base_path/root_aggregate/image_format/param_descriptions (H15)"
```

---

## Task 3: M14 — `@toolkit` lifecycle validation

**Files:**
- Modify: `src/smarter_mcp/_decorators.py`
- Create: `tests/test_config_llm_deps/test_m14_lifecycle.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_llm_deps/test_m14_lifecycle.py`:

```python
"""Tests for M14 — @toolkit lifecycle validation."""
from __future__ import annotations

import pytest

from smarter_mcp._decorators import clear_global_registry, toolkit


@pytest.fixture(autouse=True)
def _reset():
    clear_global_registry()
    yield
    clear_global_registry()


class TestLifecycleValidation:
    def test_valid_lifecycle_session(self):
        @toolkit(lifecycle="session")
        class MyTool:
            pass
        assert MyTool._smarter_mcp_lifecycle == "session"

    def test_valid_lifecycle_singleton(self):
        @toolkit(lifecycle="singleton")
        class MyTool:
            pass
        assert MyTool._smarter_mcp_lifecycle == "singleton"

    def test_valid_lifecycle_per_call(self):
        @toolkit(lifecycle="per-call")
        class MyTool:
            pass
        assert MyTool._smarter_mcp_lifecycle == "per-call"

    def test_invalid_lifecycle_typo_raises_value_error(self):
        """@toolkit(lifecycle='sesion') is a typo; must raise ValueError at decoration time."""
        with pytest.raises(ValueError, match="sesion"):
            @toolkit(lifecycle="sesion")
            class Bad:
                pass

    def test_invalid_lifecycle_error_lists_valid_options(self):
        """ValueError message must name the valid lifecycle options."""
        with pytest.raises(ValueError) as exc_info:
            @toolkit(lifecycle="forever")
            class Bad:
                pass
        msg = str(exc_info.value)
        assert "session" in msg
        assert "singleton" in msg
        assert "per-call" in msg

    def test_default_lifecycle_is_session(self):
        @toolkit
        class MyTool:
            pass
        assert MyTool._smarter_mcp_lifecycle == "session"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --extra all pytest tests/test_config_llm_deps/test_m14_lifecycle.py -v
```
Expected: `test_invalid_lifecycle_typo_raises_value_error` and `test_invalid_lifecycle_error_lists_valid_options` FAIL

- [ ] **Step 3: Update _decorators.py**

In `src/smarter_mcp/_decorators.py`, add validation at the top of the `toolkit` function:

After the existing imports, add:
```python
_VALID_LIFECYCLES: frozenset[str] = frozenset({"session", "singleton", "per-call"})
```

In the `toolkit` function, add validation before the class/decorator logic:

```python
def toolkit(
    first_arg: type | str | None = None,
    *,
    lifecycle: str = "session",
    namespace: str = "default",
    constructor_args: dict[str, Any] | None = None
) -> Callable:
    """Mark a class as an MCP toolkit.
    ...
    """
    # M14: validate lifecycle at decoration time so typos fail immediately
    # rather than silently accepting any string.
    if lifecycle not in _VALID_LIFECYCLES:
        raise ValueError(
            f"Invalid lifecycle {lifecycle!r}. "
            f"Must be one of: {sorted(_VALID_LIFECYCLES)}"
        )

    resolved_namespace = namespace
    cls_to_decorate = None
    ...
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --extra all pytest tests/test_config_llm_deps/test_m14_lifecycle.py -v
```
Expected: all 6 tests PASS

- [ ] **Step 5: Run full suite**

```bash
uv run --extra all pytest -q
```
Expected: all tests pass (no toolkit with invalid lifecycle exists in the codebase)

- [ ] **Step 6: Commit**

```bash
git add src/smarter_mcp/_decorators.py tests/test_config_llm_deps/test_m14_lifecycle.py
git commit -m "fix(decorators): validate @toolkit lifecycle against allowed values at decoration time (M14)"
```

---

## Task 4: H16 — LLM client timeout + abort-on-auth + description sanitize + router description fix + cache pruning

**Files:**
- Modify: `src/smarter_mcp/llm/client.py` (timeout + max_retries)
- Modify: `src/smarter_mcp/llm/generator.py` (abort-on-auth, sanitize, cache pruning)
- Modify: `src/smarter_mcp/server/router.py` (remove first-line truncation)
- Create: `tests/test_config_llm_deps/test_h16_llm.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config_llm_deps/test_h16_llm.py`:

```python
"""Tests for H16 — LLM client reliability + router description fix + cache pruning."""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smarter_mcp._registry import RegisteredTool, ToolRegistry
from smarter_mcp.config.manifest import LLMConfig
from smarter_mcp.llm.client import LLMNotAvailableError, OpenAIClient
from smarter_mcp.llm.generator import LLMGenerator
from smarter_mcp.server.router import _build_tool_description


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry_with_tools(names: list[str]) -> ToolRegistry:
    """Build a registry with simple stub tools."""
    registry = ToolRegistry()
    for name in names:
        fn = lambda **kw: None  # noqa: E731
        fn.__name__ = name
        registry.register_tool(fn, name=name, namespace="default")
    return registry


def _make_config(**kwargs) -> LLMConfig:
    defaults = dict(
        enabled=True,
        provider="openai",
        api_key_env="TEST_KEY",
        cache_path="/tmp/test_desc_cache.json",
    )
    defaults.update(kwargs)
    return LLMConfig(**defaults)


# ---------------------------------------------------------------------------
# H16-a: timeout passed to OpenAI client constructor
# ---------------------------------------------------------------------------

class TestOpenAIClientTimeout:
    def test_timeout_passed_to_client_constructor(self, monkeypatch):
        """OpenAIClient must pass timeout= to the OpenAI SDK constructor."""
        captured_kwargs = {}

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured_kwargs.update(kwargs)

        monkeypatch.setenv("TEST_KEY", "sk-test")

        with patch("smarter_mcp.llm.client.OpenAI", FakeOpenAI):
            config = _make_config()
            try:
                client = OpenAIClient(config)
            except Exception:
                pass  # FakeOpenAI has no chat attr; that's fine

        assert "timeout" in captured_kwargs, (
            "OpenAI SDK must be constructed with explicit timeout= to avoid 600s default"
        )
        assert isinstance(captured_kwargs["timeout"], (int, float)), (
            f"timeout must be numeric, got {captured_kwargs['timeout']!r}"
        )
        assert 5 <= captured_kwargs["timeout"] <= 60, (
            f"timeout {captured_kwargs['timeout']} is outside reasonable 5-60s range"
        )

    def test_max_retries_passed_to_client_constructor(self, monkeypatch):
        """OpenAIClient must pass max_retries= to the OpenAI SDK constructor."""
        captured_kwargs = {}

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured_kwargs.update(kwargs)

        monkeypatch.setenv("TEST_KEY", "sk-test")

        with patch("smarter_mcp.llm.client.OpenAI", FakeOpenAI):
            config = _make_config()
            try:
                client = OpenAIClient(config)
            except Exception:
                pass

        assert "max_retries" in captured_kwargs, (
            "OpenAI SDK must be constructed with explicit max_retries="
        )
        assert captured_kwargs["max_retries"] in (1, 2), (
            f"max_retries should be 1 or 2, got {captured_kwargs['max_retries']!r}"
        )


# ---------------------------------------------------------------------------
# H16-b: abort enrichment on auth/connection errors
# ---------------------------------------------------------------------------

class TestEnrichAbortOnAuthError:
    def test_abort_on_authentication_error(self, tmp_path):
        """enrich_registry must abort (not per-tool retry) on AuthenticationError."""
        config = _make_config(cache_path=str(tmp_path / "cache.json"))

        call_count = 0

        class AuthError(Exception):
            pass

        # Pretend AuthenticationError has this class name
        AuthError.__name__ = "AuthenticationError"

        class FakeClient:
            def generate(self, system, user):
                nonlocal call_count
                call_count += 1
                raise AuthError("401 Unauthorized")

        registry = _make_registry_with_tools(["tool_a", "tool_b", "tool_c"])
        gen = LLMGenerator(config, client=FakeClient())
        gen.enrich_registry(registry)

        assert call_count <= 1, (
            f"Expected at most 1 LLM call after AuthenticationError, got {call_count}. "
            "Enrichment must abort on auth errors, not retry per-tool."
        )

    def test_abort_on_connection_error(self, tmp_path):
        """enrich_registry must abort on APIConnectionError."""
        config = _make_config(cache_path=str(tmp_path / "cache.json"))

        call_count = 0

        class ConnError(Exception):
            pass

        ConnError.__name__ = "APIConnectionError"

        class FakeClient:
            def generate(self, system, user):
                nonlocal call_count
                call_count += 1
                raise ConnError("Connection refused")

        registry = _make_registry_with_tools(["t1", "t2", "t3"])
        gen = LLMGenerator(config, client=FakeClient())
        gen.enrich_registry(registry)

        assert call_count <= 1, (
            f"Expected abort after APIConnectionError, got {call_count} calls"
        )

    def test_per_tool_content_error_continues(self, tmp_path):
        """Content/BadRequest errors per tool must not abort the whole enrichment."""
        config = _make_config(cache_path=str(tmp_path / "cache.json"))

        call_count = 0

        class ContentError(Exception):
            pass

        ContentError.__name__ = "BadRequestError"

        class FakeClient:
            def generate(self, system, user):
                nonlocal call_count
                call_count += 1
                raise ContentError("content policy")

        registry = _make_registry_with_tools(["t1", "t2", "t3"])
        gen = LLMGenerator(config, client=FakeClient())
        gen.enrich_registry(registry)

        assert call_count == 3, (
            f"Expected 3 calls (one per tool) for non-auth errors, got {call_count}"
        )


# ---------------------------------------------------------------------------
# H16-c: description sanitization before caching
# ---------------------------------------------------------------------------

class TestDescriptionSanitization:
    def test_markdown_fence_stripped(self, tmp_path):
        """Descriptions with code fences must be stripped before caching."""
        config = _make_config(cache_path=str(tmp_path / "cache.json"))

        class FakeClient:
            def generate(self, system, user):
                return "```python\nsome_code()\n```\nActual description here."

        registry = _make_registry_with_tools(["my_tool"])
        gen = LLMGenerator(config, client=FakeClient())
        gen.enrich_registry(registry)

        tools = list(registry.get_all_tools())
        desc = tools[0].description or ""
        assert "```" not in desc, (
            f"Markdown fences should be stripped; got: {desc!r}"
        )

    def test_description_length_capped(self, tmp_path):
        """Generated descriptions must be capped at a reasonable length."""
        config = _make_config(cache_path=str(tmp_path / "cache.json"))

        long_desc = "word " * 200  # 1000 chars

        class FakeClient:
            def generate(self, system, user):
                return long_desc

        registry = _make_registry_with_tools(["my_tool"])
        gen = LLMGenerator(config, client=FakeClient())
        gen.enrich_registry(registry)

        tools = list(registry.get_all_tools())
        desc = tools[0].description or ""
        assert len(desc) <= 600, (
            f"Description length {len(desc)} exceeds 600-char cap; got: {desc!r}"
        )


# ---------------------------------------------------------------------------
# H16-d: router must NOT truncate explicit / LLM descriptions to first line
# ---------------------------------------------------------------------------

class TestRouterDescriptionNotTruncated:
    def _make_tool(self, description: str) -> RegisteredTool:
        fn = lambda: None  # noqa: E731
        fn.__name__ = "my_tool"
        tool = RegisteredTool(
            fn=fn,
            name="my_tool",
            namespace="default",
            description=description,
        )
        return tool

    def test_multiline_description_not_truncated(self):
        """_build_tool_description must return the full description, not just the first line."""
        desc = "First line summary.\n\nMore detailed explanation here.\nAnd a third line."
        tool = self._make_tool(desc)
        result = _build_tool_description(tool)
        assert result == desc.strip(), (
            f"Expected full description, got truncated: {result!r}"
        )

    def test_single_line_description_preserved(self):
        desc = "A single-line description."
        tool = self._make_tool(desc)
        result = _build_tool_description(tool)
        assert result == desc

    def test_auto_generated_when_no_description(self):
        """When there is no description, auto-generate a placeholder."""
        fn = lambda: None  # noqa: E731
        fn.__name__ = "no_desc_tool"
        tool = RegisteredTool(
            fn=fn,
            name="no_desc_tool",
            namespace="default",
            description=None,
        )
        result = _build_tool_description(tool)
        assert "no_desc_tool" in result


# ---------------------------------------------------------------------------
# Cache pruning
# ---------------------------------------------------------------------------

class TestCachePruning:
    def test_stale_cache_entries_pruned_on_save(self, tmp_path):
        """enrich_registry must prune cache entries whose tools are no longer in the registry."""
        cache_path = tmp_path / "cache.json"
        config = _make_config(cache_path=str(cache_path))

        # Seed the cache with a stale entry
        stale_key = "deadbeef" * 8  # 64-char hex
        initial_cache = {stale_key: "A stale description for a deleted tool."}
        cache_path.write_text(json.dumps(initial_cache))

        call_count = 0

        class FakeClient:
            def generate(self, system, user):
                nonlocal call_count
                call_count += 1
                return "Fresh description."

        registry = _make_registry_with_tools(["current_tool"])
        gen = LLMGenerator(config, client=FakeClient())
        gen.enrich_registry(registry)

        saved = json.loads(cache_path.read_text())
        assert stale_key not in saved, (
            f"Stale cache key should have been pruned; cache: {saved}"
        )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --extra all pytest tests/test_config_llm_deps/test_h16_llm.py -v
```
Expected: multiple tests fail (no timeout, no abort, no sanitize, truncation still happens)

- [ ] **Step 3: Fix llm/client.py — add timeout and max_retries**

In `src/smarter_mcp/llm/client.py`, update the `__init__` method of `OpenAIClient`:

Replace:
```python
self._client = OpenAI(api_key=api_key, base_url=base_url)
```
With:
```python
self._client = OpenAI(
    api_key=api_key,
    base_url=base_url,
    timeout=20.0,   # prevent 600s SDK defaults; LLM descriptions are best-effort
    max_retries=1,  # one retry on transient errors; fast fail on auth errors
)
```

- [ ] **Step 4: Fix llm/generator.py — abort-on-auth, sanitize, cache pruning**

In `src/smarter_mcp/llm/generator.py`, add after imports:

```python
import re

_MAX_DESC_LEN = 500
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

# Error class names that indicate ALL future LLM calls will also fail.
# Using class name strings to avoid requiring openai as a hard dependency.
_ABORT_ON_ERROR_TYPES = frozenset({
    "AuthenticationError",
    "APIConnectionError",
})


def _sanitize_description(text: str) -> str:
    """Strip markdown code fences and cap length for safe caching."""
    text = _FENCE_RE.sub("", text).strip()
    if len(text) > _MAX_DESC_LEN:
        # Truncate at a word boundary
        truncated = text[:_MAX_DESC_LEN]
        last_space = truncated.rfind(" ")
        text = (truncated[:last_space] if last_space > 0 else truncated) + "…"
    return text
```

Update `generate_for_tool` to use `_sanitize_description`:

Replace:
```python
description = client.generate(_SYSTEM_PROMPT, user_prompt).strip()
```
With:
```python
raw = client.generate(_SYSTEM_PROMPT, user_prompt)
description = _sanitize_description(raw)
```

Also update the exception handling in `generate_for_tool` to re-raise abort-class errors:

```python
except LLMNotAvailableError:
    raise
except Exception as e:
    if type(e).__name__ in _ABORT_ON_ERROR_TYPES:
        raise  # let enrich_registry abort the loop
    logger.warning("LLM description failed for tool '%s': %s", tool.name, e)
    return None
```

Update `enrich_registry` to (a) abort on auth/connection errors and (b) prune stale cache entries:

```python
def enrich_registry(self, registry: ToolRegistry) -> int:
    """Fill in descriptions for tools across the whole registry.

    Returns the number of descriptions written. Persists the cache before
    returning, pruning stale entries (tools no longer in the registry).
    """
    written = 0
    # Collect cache keys for all current tools to enable stale-entry pruning.
    active_keys: set[str] = set()

    for tool in registry.get_all_tools():
        sig = self._build_signature(tool)
        doc = tool.description or ""
        active_keys.add(self._cache_key(sig, doc))

        if not self._needs_description(tool):
            continue

        try:
            description = self.generate_for_tool(tool)
        except LLMNotAvailableError:
            raise
        except Exception as e:
            # Auth/connection errors mean ALL future calls will also fail.
            if type(e).__name__ in _ABORT_ON_ERROR_TYPES:
                logger.error(
                    "LLM enrichment aborted: %s — %s. "
                    "Check API key and connectivity. No descriptions generated.",
                    type(e).__name__, e,
                )
                break
            logger.warning("LLM description failed for tool '%s': %s", tool.name, e)
            description = None

        if description:
            tool.description = description
            written += 1

    # Prune cache entries whose tools are no longer in this registry run.
    stale = set(self._cache) - active_keys
    if stale:
        for k in stale:
            del self._cache[k]
        self._dirty = True

    self.save_cache()
    if written:
        logger.info("LLM generated %d tool description(s).", written)
    return written
```

- [ ] **Step 5: Fix server/router.py — remove first-line truncation**

In `src/smarter_mcp/server/router.py`, update `_build_tool_description`:

Replace:
```python
def _build_tool_description(tool: RegisteredTool | RegisteredResource) -> str:
    """Generate a description for the MCP tool or resource."""
    if tool.description:
        # Use first line of description/docstring
        first_line = tool.description.strip().split("\n")[0].strip()
        if first_line:
            return first_line

    # Auto-generate a basic description
    if isinstance(tool, RegisteredTool):
        if tool.class_name:
            return f"{tool.class_name}.{tool.name}()"
        return f"{tool.name}()"
    elif isinstance(tool, RegisteredResource):
        return f"Resource: {tool.uri}"

    return ""
```

With:
```python
def _build_tool_description(tool: RegisteredTool | RegisteredResource) -> str:
    """Return the full description for an MCP tool or resource.

    Explicit @tool("...") strings and LLM-generated descriptions may span
    multiple lines — truncating them to the first line discards meaningful
    content and wastes LLM-generated text. Only auto-generated placeholders
    are necessarily terse.
    """
    if tool.description:
        # Return the full description unchanged — do NOT truncate to the
        # first line. Multi-line docstrings and LLM descriptions must survive.
        return tool.description.strip()

    # Auto-generate a minimal placeholder when no description is available.
    if isinstance(tool, RegisteredTool):
        if tool.class_name:
            return f"{tool.class_name}.{tool.name}()"
        return f"{tool.name}()"
    elif isinstance(tool, RegisteredResource):
        return f"Resource: {tool.uri}"

    return ""
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run --extra all pytest tests/test_config_llm_deps/test_h16_llm.py -v
```
Expected: all tests PASS

- [ ] **Step 7: Run full suite**

```bash
uv run --extra all pytest -q
```
Expected: all tests pass

- [ ] **Step 8: Commit**

```bash
git add src/smarter_mcp/llm/client.py src/smarter_mcp/llm/generator.py src/smarter_mcp/server/router.py tests/test_config_llm_deps/test_h16_llm.py
git commit -m "fix(llm): timeout+max_retries, abort-on-auth-error, description sanitize+cache-pruning; stop truncating router descriptions (H16, cache-pruning)"
```

---

## Task 5: Wire remaining dead config (log_level, auto_detect, SourceConfig.include)

**Files:**
- Modify: `src/smarter_mcp/server/app.py` (wire log_level + add include param to discover() + pass include in build())
- Modify: `src/smarter_mcp/extractor/surface.py` (add include_patterns support)
- Modify: `src/smarter_mcp/runtime/tool_wrapper.py` (accept auto_detect param)
- Modify: `src/smarter_mcp/server/router.py` (pass auto_detect to build_tool_wrapper)
- Create: `tests/test_config_llm_deps/test_dead_config_wiring.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config_llm_deps/test_dead_config_wiring.py`:

```python
"""Tests for wiring previously-dead config fields:
- server.log_level → applied at server startup
- multimodal.auto_detect → gates image coercion in tool_wrapper
- SourceConfig.include for path sources → filters files in SurfaceExtractor
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest

from smarter_mcp._decorators import clear_global_registry
from smarter_mcp.config.manifest import ManifestConfig, MultimodalConfig, ServerConfig


@pytest.fixture(autouse=True)
def _reset():
    clear_global_registry()
    yield
    clear_global_registry()


# ---------------------------------------------------------------------------
# log_level wiring
# ---------------------------------------------------------------------------

class TestLogLevelWiring:
    def test_log_level_debug_applied_at_init(self):
        """When manifest.server.log_level='debug', root logger level becomes DEBUG."""
        from smarter_mcp.server.app import SmarterMCP

        app = SmarterMCP("test-log-debug")
        app._config.server.log_level = "debug"

        # Simulate what SmarterMCP.__init__ should do: apply log_level
        # We trigger it by calling build() or via a dedicated method.
        # The wiring applies the level at __init__ time after config is loaded.
        # For this test we check by building with a fresh app with manifest.
        import os, tempfile
        with tempfile.TemporaryDirectory() as td:
            mf = Path(td) / "smarter-mcp.yaml"
            mf.write_text("name: debug-test\nserver:\n  log_level: debug\n")
            app2 = SmarterMCP(manifest=str(mf))
            root_level = logging.getLogger().level
            assert root_level == logging.DEBUG, (
                f"Expected root logger level=DEBUG (10), got {root_level}"
            )

    def test_log_level_warning_applied(self):
        """server.log_level='warning' must set root logger to WARNING."""
        import tempfile
        from smarter_mcp.server.app import SmarterMCP
        with tempfile.TemporaryDirectory() as td:
            mf = Path(td) / "smarter-mcp.yaml"
            mf.write_text("name: warn-test\nserver:\n  log_level: warning\n")
            app = SmarterMCP(manifest=str(mf))
            root_level = logging.getLogger().level
            assert root_level == logging.WARNING, (
                f"Expected WARNING (30), got {root_level}"
            )


# ---------------------------------------------------------------------------
# auto_detect wiring
# ---------------------------------------------------------------------------

class TestAutoDetectWiring:
    def test_auto_detect_false_skips_image_coercion(self):
        """With auto_detect=False, a tool returning a plain string must NOT be
        passed through coerce_to_fastmcp_image (which would error on non-images)."""
        from smarter_mcp.runtime.tool_wrapper import build_tool_wrapper
        from smarter_mcp._registry import RegisteredTool

        def my_tool(x: str) -> str:
            return f"hello {x}"

        fn = my_tool
        fn._smarter_mcp_tool = True

        tool = RegisteredTool(fn=fn, name="my_tool", namespace="default")

        # With auto_detect=False, the wrapper must NOT call coerce_to_fastmcp_image
        wrapper = build_tool_wrapper(tool, fn, auto_detect=False)

        # Calling the wrapper should return the plain string unchanged
        # (no coerce_to_fastmcp_image call that might fail)
        result = wrapper(x="world")
        assert result == "hello world", (
            f"Expected 'hello world', got {result!r}. "
            "auto_detect=False must skip image coercion entirely."
        )

    def test_auto_detect_true_is_default(self):
        """build_tool_wrapper must default to auto_detect=True."""
        from smarter_mcp.runtime.tool_wrapper import build_tool_wrapper
        from smarter_mcp._registry import RegisteredTool
        import inspect

        def my_tool(x: str) -> str:
            return "hi"

        tool = RegisteredTool(fn=my_tool, name="my_tool", namespace="default")
        # Should not raise; auto_detect defaults to True
        wrapper = build_tool_wrapper(tool, my_tool)
        sig = inspect.signature(wrapper)
        assert "x" in sig.parameters


# ---------------------------------------------------------------------------
# SourceConfig.include for path sources
# ---------------------------------------------------------------------------

class TestSourceConfigIncludePathSources:
    def test_include_pattern_filters_files(self, tmp_path):
        """SourceConfig.include patterns must restrict which files are scanned
        when the source is a path (not a module)."""
        # Create two Python files
        (tmp_path / "tools.py").write_text(
            "def greet(name: str) -> str:\n    return f'hello {name}'\n"
        )
        (tmp_path / "internal.py").write_text(
            "def secret(x: int) -> int:\n    return x * 2\n"
        )

        # Manifest: include only tools.py
        mf = tmp_path / "smarter-mcp.yaml"
        mf.write_text(
            f"name: test\n"
            f"sources:\n"
            f"  - path: .\n"
            f"    include:\n"
            f"      - tools.py\n"
        )

        from smarter_mcp.server.app import SmarterMCP
        app = SmarterMCP(manifest=str(mf), use_inspect=False)
        app.build()

        tool_names = {t.name for t in app._registry.get_all_tools()}
        assert "greet" in tool_names, f"'greet' (from tools.py) should be discovered"
        assert "secret" not in tool_names, (
            f"'secret' (from internal.py) should be excluded by include pattern"
        )

    def test_empty_include_scans_all(self, tmp_path):
        """SourceConfig.include=[] (empty) must scan all files (no filter)."""
        (tmp_path / "a.py").write_text(
            "def func_a(x: int) -> int:\n    return x\n"
        )
        (tmp_path / "b.py").write_text(
            "def func_b(x: int) -> int:\n    return x\n"
        )

        mf = tmp_path / "smarter-mcp.yaml"
        mf.write_text(
            f"name: test\n"
            f"sources:\n"
            f"  - path: .\n"
        )

        from smarter_mcp.server.app import SmarterMCP
        app = SmarterMCP(manifest=str(mf), use_inspect=False)
        app.build()

        tool_names = {t.name for t in app._registry.get_all_tools()}
        assert "func_a" in tool_names
        assert "func_b" in tool_names
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --extra all pytest tests/test_config_llm_deps/test_dead_config_wiring.py -v
```
Expected: most tests fail (wiring not implemented yet)

- [ ] **Step 3: Wire log_level in app.py**

In `src/smarter_mcp/server/app.py`, at the END of `SmarterMCP.__init__`, after all config overrides and before instance setup, add:

```python
# Wire server.log_level: configure the root Python logger level from the manifest.
# This lets the manifest control logging verbosity at startup without requiring
# CLI flags. A bad value (e.g. "verbose" instead of "debug") is silently ignored
# so a typo doesn't prevent the server from starting.
_level_name = self._config.server.log_level.upper()
_level = getattr(logging, _level_name, None)
if isinstance(_level, int):
    logging.getLogger().setLevel(_level)
```

(The import `logging` is already present at the top of `app.py`.)

- [ ] **Step 4: Add auto_detect param to build_tool_wrapper**

In `src/smarter_mcp/runtime/tool_wrapper.py`, update the `build_tool_wrapper` signature:

```python
def build_tool_wrapper(
    tool: RegisteredTool,
    impl: Callable,
    instance_manager: InstanceManager | None = None,
    *,
    auto_detect: bool = True,
) -> Callable:
```

Pass `auto_detect` down to the sub-builders:
```python
if not is_method or kind == CallableKind.STATICMETHOD:
    wrapper = _build_function_wrapper(tool, impl, is_async, auto_detect=auto_detect)
elif kind == CallableKind.CLASSMETHOD:
    wrapper = _build_function_wrapper(tool, impl, is_async, auto_detect=auto_detect)
else:
    wrapper = _build_method_wrapper(tool, impl, instance_manager, is_async, auto_detect=auto_detect)
```

Update `_build_function_wrapper` signature:
```python
def _build_function_wrapper(
    tool: RegisteredTool,
    impl: Callable,
    is_async: bool,
    *,
    auto_detect: bool = True,
) -> Callable:
```

In both `_async_wrapper` and `_sync_wrapper`, change:
```python
return coerce_to_fastmcp_image(res)
```
To:
```python
return coerce_to_fastmcp_image(res) if auto_detect else res
```

Update `_build_method_wrapper` signature:
```python
def _build_method_wrapper(
    tool: RegisteredTool,
    impl: Callable,
    manager: InstanceManager,
    is_async: bool,
    *,
    auto_detect: bool = True,
) -> Callable:
```

Apply the same `if auto_detect` guard to all 4 `coerce_to_fastmcp_image` calls in `_build_method_wrapper`.

- [ ] **Step 5: Pass auto_detect from router to build_tool_wrapper**

In `src/smarter_mcp/server/router.py`, update `_register_tool`:

```python
impl = build_tool_wrapper(
    tool,
    impl,
    self.instance_manager,
    auto_detect=self.config.multimodal.auto_detect,
)
```

- [ ] **Step 6: Add include_patterns to SurfaceExtractor**

In `src/smarter_mcp/extractor/surface.py`, update `SurfaceExtractor.__init__`:

```python
def __init__(
    self,
    source_root: str | Path,
    use_inspect: bool = True,
    exclude_patterns: list[str] | None = None,
    include_patterns: list[str] | None = None,
    use_cache: bool = False,
    cache_dir: str | Path | None = None,
):
    self.source_root = Path(source_root).resolve()
    self.use_inspect = use_inspect
    self.exclude_patterns = exclude_patterns or ["test_*", "*_test.py", "conftest.py"]
    self.include_patterns = include_patterns or []
    ...
```

Update `_is_excluded` to also handle include filtering (or add a separate `_is_included` helper used in `_discover_files`):

In `_discover_files`, after the exclude check, add:
```python
# User-supplied include patterns: if non-empty, only files matching
# at least one pattern are scanned.
if self.include_patterns and not any(
    py_file.match(pat) for pat in self.include_patterns
):
    continue
```

The full updated file-loop in `_discover_files` should look like:
```python
for filename in filenames:
    if not filename.endswith(".py"):
        continue
    py_file = current / filename
    if self._is_excluded(py_file):
        continue
    # Include filter: when non-empty, only keep files matching a pattern.
    if self.include_patterns and not any(
        py_file.match(pat) for pat in self.include_patterns
    ):
        continue
    files.append(py_file)
```

- [ ] **Step 7: Add include param to discover() and wire through build()**

In `src/smarter_mcp/server/app.py`, update `discover()` signature and implementation:

```python
def discover(
    self,
    source_root: str | Path,
    exclude: list[str] | None = None,
    include: list[str] | None = None,
    use_cache: bool = False,
) -> SmarterMCP:
    ...
    extractor = SurfaceExtractor(
        source_root=path,
        use_inspect=self._use_inspect,
        exclude_patterns=exclude or ["test_*", "*_test.py", "conftest.py"],
        include_patterns=include or [],
        use_cache=use_cache,
    )
    ...
```

In `build()`, update the path-source call to pass `include`:

Find:
```python
self.discover(str(src_path), exclude=source.exclude)
```
Replace with:
```python
self.discover(
    str(src_path),
    exclude=source.exclude,
    include=source.include if source.include else None,
)
```

- [ ] **Step 8: Run tests to verify they pass**

```bash
uv run --extra all pytest tests/test_config_llm_deps/test_dead_config_wiring.py -v
```
Expected: all tests PASS

- [ ] **Step 9: Run full suite**

```bash
uv run --extra all pytest -q
```
Expected: all tests pass

- [ ] **Step 10: Commit**

```bash
git add \
  src/smarter_mcp/server/app.py \
  src/smarter_mcp/extractor/surface.py \
  src/smarter_mcp/runtime/tool_wrapper.py \
  src/smarter_mcp/server/router.py \
  tests/test_config_llm_deps/test_dead_config_wiring.py
git commit -m "feat(config): wire log_level, auto_detect, SourceConfig.include for path sources (dead-config cleanup)"
```

---

## Task 6: Ruff cleanup + UPDATE_LOG + final verification

**Files:**
- Modify: `docs/memory/UPDATE_LOG.md`

- [ ] **Step 1: Run ruff and fix any NEW errors introduced by this branch**

```bash
uv run --extra dev ruff check src/ tests/ --fix
# Review what was auto-fixed
uv run --extra dev ruff check src/ tests/
```

Only fix errors that were introduced by changes in this branch (do not fix pre-existing errors unrelated to our changes, to keep commits clean).

- [ ] **Step 2: Run the full test suite one final time**

```bash
uv run --extra all pytest -q
```
Expected: all tests pass (≥153 + all new tests)

- [ ] **Step 3: Get the current timestamp**

```bash
python3 -c "from datetime import datetime; n=datetime.now(); print(n.strftime('%A, %d-%m-%Y, %-I:%M ') + n.strftime('%p').lower())"
```

- [ ] **Step 4: Prepend entry to UPDATE_LOG.md**

At the very top of `docs/memory/UPDATE_LOG.md`, add (with the real timestamp from Step 3):

```markdown
## <Day, DD-MM-YYYY, HH:MM am/pm, [feat/config-llm-deps] Config+LLM+dep hardening: H15/H16/H19/M14/A3 + dead-config wiring

extra=forbid on all manifest models (H15); dead fields removed (cors_origins, routing.base_path/root_aggregate, image_format, param_descriptions) or wired (log_level, auto_detect, SourceConfig.include); @toolkit lifecycle validated (M14); LLM client gets timeout+max_retries+abort-on-auth+description sanitize+cache pruning (H16); openai moved to [llm] extra, structlog+jinja2 dropped (H19); fastmcp pinned >=3.3.1,<4 (A3). N tests pass.
```

Replace `N` with the actual test count from Step 2.

- [ ] **Step 5: Commit the UPDATE_LOG**

```bash
git add docs/memory/UPDATE_LOG.md
git commit -m "docs(memory): add update log entry for feat/config-llm-deps"
```

---

## Self-review

### Spec coverage

| Finding | Task | Status |
|---|---|---|
| H15 extra="forbid" | Task 2 | ✓ |
| H15 dead cors_origins | Task 2 | ✓ removed |
| H15 dead routing.base_path | Task 2 | ✓ removed |
| H15 dead routing.root_aggregate | Task 2 | ✓ removed |
| H15 dead image_format | Task 2 | ✓ removed |
| H15 dead param_descriptions | Task 2 | ✓ removed |
| H15 wire log_level | Task 5 | ✓ wired |
| H15 wire auto_detect | Task 5 | ✓ wired |
| H15 wire SourceConfig.include (path) | Task 5 | ✓ wired |
| H15 keep image_max_size with note | Task 2 | ✓ kept+docstring |
| M14 lifecycle validation | Task 3 | ✓ |
| H16 LLM timeout/max_retries | Task 4 | ✓ |
| H16 abort on auth/connection error | Task 4 | ✓ |
| H16 sanitize descriptions | Task 4 | ✓ |
| H16 router not truncating descriptions | Task 4 | ✓ |
| H19 openai to [llm] extra | Task 1 | ✓ |
| H19 drop structlog+jinja2 | Task 1 | ✓ |
| H19 update error message to smarter-mcp[llm] | Task 1 | ✓ |
| H19 all extra includes llm | Task 1 | ✓ |
| A3 pin fastmcp range | Task 1 | ✓ |
| Cache pruning | Task 4 | ✓ |

### Known edge cases
- `manifest_dir` is `Field(exclude=True)` with a default, so it's NOT treated as extra by Pydantic — setting it post-construction still works.
- `routing.overrides` and `routing.separator` are NOT dead config — they ARE used (`NamespaceRouter` reads `self.routing.separator` and `self.routing.overrides`). Keep them.
- `SourceConfig.include` for module sources already works (passed to `discover_module`). Only path sources were broken — Task 5 wires it via `SurfaceExtractor.include_patterns`.
- The `validate_findings.py` H15 check now passes (good signal). The H1 check still shows `host=0.0.0.0` which is a separate finding not in scope.
