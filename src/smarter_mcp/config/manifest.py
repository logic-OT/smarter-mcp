"""
Manifest configuration — Pydantic-validated YAML config with env var substitution.

The manifest is the control plane. It defines:
- Which source files to scan
- What to expose and how
- How to instantiate classes
- Server transport and security settings
- Per-tool name/description overrides
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


# ──────────────────────────────────────────────────────────────────────
# Environment variable substitution
# ──────────────────────────────────────────────────────────────────────

_ENV_VAR_RE = re.compile(r"\$\{(\w+)(?::([^}]*))?\}")


def _substitute_env_vars(value: Any) -> Any:
    """Recursively substitute ${VAR} and ${VAR:default} in strings."""
    if isinstance(value, str):
        def _replace(match: re.Match) -> str:
            var_name = match.group(1)
            default = match.group(2)
            env_value = os.environ.get(var_name)
            if env_value is not None:
                return env_value
            if default is not None:
                return default
            raise ValueError(
                f"Environment variable ${{{var_name}}} is not set and has no default"
            )
        return _ENV_VAR_RE.sub(_replace, value)
    elif isinstance(value, dict):
        return {k: _substitute_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_substitute_env_vars(v) for v in value]
    return value


# ──────────────────────────────────────────────────────────────────────
# Config models
# ──────────────────────────────────────────────────────────────────────

class ServerConfig(BaseModel):
    """Server transport and networking configuration."""

    host: str = "0.0.0.0"
    port: int = 8000
    transport: Literal["sse", "streamable-http", "stdio"] = "sse"
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    log_level: str = "info"

    # Auth
    auth_enabled: bool = False
    auth_header: str = "X-API-Key"
    auth_keys_env: str = "SMARTER_MCP_API_KEYS"  # Comma-separated env var

    # Rate limiting
    rate_limit_enabled: bool = False
    rate_limit_per_minute: int = 60
    rate_limit_global_per_minute: int = 1000  # spec's "global: 1000/minute"


class SourceConfig(BaseModel):
    """Configuration for a source directory or module to scan."""

    path: str | None = None
    """Path relative to manifest file (or absolute)."""

    module: str | None = None
    """Importable module name to scan (e.g., 'random')."""
    
    namespace: str | None = None
    """Custom namespace override."""

    exclude: list[str] = Field(default_factory=lambda: ["test_*", "*_test.py", "conftest.py"])
    """Glob patterns to exclude from scanning."""

    include: list[str] = Field(default_factory=list)
    """If non-empty, only include files matching these patterns."""

    @model_validator(mode="after")
    def check_path_or_module(self) -> SourceConfig:
        if bool(self.path) == bool(self.module):
            raise ValueError("Exactly one of 'path' or 'module' must be provided in SourceConfig")
        return self


class RoutingConfig(BaseModel):
    """Namespace routing configuration."""

    base_path: str = "/mcp"
    """Base URL path for MCP endpoints."""

    root_aggregate: bool = True
    """Whether the root server aggregates all tools from sub-namespaces."""

    overrides: dict[str, str] = Field(default_factory=dict)
    """Module path → custom namespace mapping (e.g., 'db/client' → 'database')."""

    separator: str = "_"
    """Separator for auto-generated tool names from class methods (ClassName_method)."""


class ExposeConfig(BaseModel):
    """Controls what gets exposed as MCP tools."""

    include_private: bool = False
    include_dunder: bool = False
    include_inherited: bool = False
    include_properties: bool = True
    variadic_policy: Literal["skip", "warn", "expose"] = "warn"
    unannotated_policy: Literal["expose", "warn", "skip"] = "expose"
    respect_all: bool = True


class InstanceConfig(BaseModel):
    """Configuration for how to instantiate a class.

    Supports three instantiation strategies:
    1. Constructor args: Provide constructor arguments directly
    2. Factory function: Call a factory function
    3. Default: Try cls() with no arguments
    """

    class_name: str
    """Fully qualified class name (e.g., 'mylib.db.Client')."""

    lifecycle: Literal["session", "singleton", "per-call"] = "session"
    """Instance lifecycle: session (per-connection), singleton, or per-call."""

    constructor_args: dict[str, Any] = Field(default_factory=dict)
    """Arguments to pass to the constructor."""

    factory: str | None = None
    """Factory function name (e.g., 'mylib.db.create_client')."""

    factory_args: dict[str, Any] = Field(default_factory=dict)
    """Arguments to pass to the factory function."""


class ToolOverride(BaseModel):
    """Per-tool customization."""

    function: str
    """Qualified function/method name (e.g., 'mylib.db.Client.query')."""

    name: str | None = None
    """Custom MCP tool name. If None, uses auto-generated name."""

    description: str | None = None
    """Custom description. Overrides docstring."""

    expose: bool = True
    """Set to false to explicitly exclude this tool."""

    param_descriptions: dict[str, str] = Field(default_factory=dict)
    """Per-parameter description overrides."""

    tests: list[dict[str, Any]] = Field(default_factory=list)
    """List of test cases for this tool."""


class MultimodalConfig(BaseModel):
    """Multimodal content handling configuration."""

    auto_detect: bool = True
    """Automatically detect PIL.Image, numpy.ndarray returns."""

    image_format: str = "png"
    """Default format for encoding images."""

    image_max_size: tuple[int, int] = (1024, 1024)
    """Maximum image dimensions (will resize if larger)."""


class LLMConfig(BaseModel):
    """LLM-assisted description generation configuration."""

    enabled: bool = False
    """Enable LLM description generation."""

    provider: str = "openrouter"
    """LLM provider: 'openai', 'openrouter', or 'anthropic'/'claude'.

    All providers use the OpenAI SDK. 'openrouter' sets
    base_url to https://openrouter.ai/api/v1 automatically; 'anthropic'/'claude'
    uses Anthropic's OpenAI-compatible endpoint. Set a custom base_url to reach
    any other OpenAI-compatible API.
    """

    model: str = "google/gemini-2.0-flash-001"
    """Model to use for description generation."""

    api_key_env: str | None = None
    """Environment variable containing the API key.

    If None, a sensible default is chosen per-provider (OPENROUTER_API_KEY,
    OPENAI_API_KEY, ANTHROPIC_API_KEY).
    """

    base_url: str | None = None
    """Override the API base URL. If None, a per-provider default is used."""

    max_tokens: int = 256
    """Maximum tokens to generate per description."""

    temperature: float = 0.2
    """Sampling temperature for description generation."""

    cache_path: str = ".smarter-mcp/description-cache.json"
    """Path for caching generated descriptions."""

    overwrite_existing: bool = False
    """Whether to overwrite existing docstrings/descriptions."""


class ManifestConfig(BaseModel):
    """Top-level manifest configuration.

    This is the Pydantic model for the smarter-mcp.yaml file.
    All fields have sensible defaults — the tool works with
    zero configuration.
    """

    name: str = "my-mcp-server"
    version: str = "0.1.0"
    description: str = ""

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
        """Coerce None to [] for list fields.

        YAML parses a bare key with no value (e.g. ``tools:``) as ``None``.
        Pydantic rejects ``None`` for ``list`` fields, so we normalise here
        before validation runs.
        """
        if isinstance(data, dict):
            for field in ("sources", "instances", "tools"):
                if data.get(field) is None:
                    data[field] = []
        return data


# ──────────────────────────────────────────────────────────────────────
# Loading
# ──────────────────────────────────────────────────────────────────────

def load_manifest(path: str | Path) -> ManifestConfig:
    """Load and validate a manifest from a YAML file.

    Args:
        path: Path to the YAML manifest file.

    Returns:
        Validated ManifestConfig.

    Raises:
        FileNotFoundError: If the manifest file doesn't exist.
        ValueError: If the manifest fails validation.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        raw = {}

    return ManifestConfig.model_validate(raw)


def default_manifest(source_path: str = ".") -> ManifestConfig:
    """Create a default manifest for a source directory.

    Used when no manifest file is provided — scans the given directory
    with all default settings.

    Args:
        source_path: Path to scan for Python files.

    Returns:
        ManifestConfig with default settings and one source entry.
    """
    return ManifestConfig(
        sources=[SourceConfig(path=source_path)],
    )


def find_manifest(search_dir: str | Path = ".") -> Path | None:
    """Search for a manifest file in the given directory and parents.

    Looks for: smarter-mcp.yaml, smarter-mcp.yml, .smarter-mcp.yaml

    Args:
        search_dir: Directory to start searching from.

    Returns:
        Path to found manifest, or None.
    """
    candidates = ["smarter-mcp.yaml", "smarter-mcp.yml", ".smarter-mcp.yaml"]
    search_dir = Path(search_dir).resolve()

    current = search_dir
    while True:
        for name in candidates:
            manifest_path = current / name
            if manifest_path.exists():
                return manifest_path

        parent = current.parent
        if parent == current:
            break
        current = parent

    return None
