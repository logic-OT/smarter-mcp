# Smarter-MCP Documentation

> Turn any Python codebase into a production-grade MCP server — with zero friction.

---

## Table of Contents

1. [What is Smarter-MCP?](#what-is-smarter-mcp)
2. [Getting Started](#getting-started)
3. [The Four Entry Points](#the-four-entry-points)
4. [The YAML Manifest](#the-yaml-manifest)
5. [The CLI](#the-cli)
6. [Multimodal Support](#multimodal-support)
7. [The Tool Testing Framework](#the-tool-testing-framework)
8. [Structured Error Handling](#structured-error-handling)
9. [HTTP Endpoints & Introspection](#http-endpoints--introspection)
10. [Authentication & Rate Limiting](#authentication--rate-limiting)
11. [LLM Description Generation](#llm-description-generation)
12. [Architecture Deep Dive](#architecture-deep-dive)
13. [Using Smarter-MCP in Another Project](#using-smarter-mcp-in-another-project)
14. [What's Implemented vs. What's Coming](#whats-implemented-vs-whats-coming)

---

## What is Smarter-MCP?

FastMCP gives you primitives — `@mcp.tool()`, manual wiring, manual schema building. Smarter-MCP gives you **abstractions**. It sits on top of FastMCP and handles everything you shouldn't have to think about:

| You used to do this manually | Smarter-MCP does it for you |
|---|---|
| Write JSON schemas for every parameter | Extracted automatically from type annotations + docstrings |
| Manually coerce string inputs from LLMs | `"42"` → `42`, `"true"` → `True`, stringified JSON → `dict` |
| Manually encode images as base64 | `PIL.Image` and `np.ndarray` returns auto-wrapped as `ImageContent` |
| Write boilerplate for class-based tools | `@toolkit(lifecycle="session")` — instances managed per-session |
| Mount sub-servers manually | Namespaces auto-derived from module paths |
| No way to test tools pre-deployment | `app.test()` or `smarter-mcp test` from the CLI |
| No way to serve existing code | `app.discover_module(random)` — one line, any module |

**Two core workflows:**

1. **You own the code** → Use decorators (`@tool()`, `@toolkit()`, `@resource()`)
2. **You don't own the code** → Point Smarter-MCP at it (`app.discover_module()`, `app.discover()`, or a YAML manifest)

Both paths converge into the same runtime engine. Every tool gets identical treatment.

---

## Getting Started

### Installation

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install with all extras (multimodal + dev tools)
pip install -e ".[all]"

# Or install just the core (no Pillow/numpy/pytest)
pip install -e .
```

### Your First Server in 30 Seconds

Create `server.py`:

```python
from smarter_mcp import SmarterMCP, tool

@tool(description="Add two numbers")
def add(a: int, b: int) -> int:
    return a + b

@tool(description="Greet someone")
def greet(name: str) -> str:
    """Say hello to a user by name."""
    return f"Hello, {name}!"

app = SmarterMCP("my-first-server")
app.run()
```

Run it:

```bash
# Serve via SSE on port 8000
python server.py

# Or use the CLI
smarter-mcp serve server.py
```

That's it. You now have an MCP server with two tools, complete with JSON schemas auto-generated from your type annotations and docstrings. An LLM agent can connect and call `add(a=2, b=3)` or `greet(name="Alice")`.

### Validate Without Starting

```bash
smarter-mcp validate server.py
```

This prints every tool, its parameters, return types, and any warnings — without starting a server. Think of it as a dry run.

### Test Your Tools

Add test cases directly to your decorators:

```python
from smarter_mcp import tool

@tool(
    description="Add two numbers",
    tests=[
        {"params": {"a": 2, "b": 3}, "expect": 5},
        {"params": {"a": -1, "b": 1}, "expect": 0},
    ]
)
def add(a: int, b: int) -> int:
    return a + b
```

Run them:

```bash
smarter-mcp test server.py
```

Output:

```
=== Running Tool Tests ===
----------------------------------------
✓ PASS  default/add (0.1ms)
✓ PASS  default/add (0.0ms)
✓ PASS  default/greet (0.0ms)
----------------------------------------
Test Summary: 3 passed, 0 failed, 0 skipped
```

---

## The Four Entry Points

Smarter-MCP gives you four ways to register tools. All four feed into the same `ToolRegistry` → `Router` → `FastMCP` pipeline.

### 1. Decorators — `@tool()`, `@toolkit()`, `@resource()`

For code you're writing from scratch.

**Functions:**

```python
from smarter_mcp import SmarterMCP, tool

@tool()
def search(query: str, limit: int = 10) -> list[dict]:
    """Search the database for matching records."""
    ...

app = SmarterMCP("my-server")
```

The description is auto-extracted from the docstring. Parameter descriptions are parsed from Google, NumPy, or Sphinx-style docstrings.

**Class-based toolkits** — for stateful tools (DB connections, API clients, ML models):

```python
from smarter_mcp import toolkit, tool

@toolkit(lifecycle="session")
class DatabaseClient:
    def __init__(self, connection_string: str = "sqlite:///db.sqlite"):
        self.conn = connect(connection_string)

    @tool()
    def query(self, sql: str) -> list[dict]:
        """Execute a SQL query and return results."""
        return self.conn.execute(sql).fetchall()

    @tool()
    def list_tables(self) -> list[str]:
        """List all tables in the database."""
        return self.conn.get_tables()
```

What `@toolkit()` handles for you:
- **Instance lifecycle**: `"session"` = one instance per MCP connection. `"singleton"` = one global instance. `"per-call"` = fresh instance every call.
- **`self` binding**: Each tool call automatically resolves the right instance and binds `self`.
- **Constructor args**: Passed via decorator kwargs, manifest YAML, or environment variables.

**Resources:**

```python
from smarter_mcp import resource

@resource("config://settings")
def get_settings() -> dict:
    return {"debug": True, "version": "1.0"}
```

### 2. `discover_module()` — turn any Python module into tools

This is the killer feature. Import *any* module and hand it to Smarter-MCP:

```python
import random
import json

app = SmarterMCP("utility-server")

app.discover_module(random, include=["choices", "randint", "sample"])
app.discover_module(json, include=["dumps", "loads"])

app.run()
```

Now an LLM can call `random.randint(a=1, b=100)` or `json.dumps(obj={"key": "value"})`. The schemas are built automatically from inspect signatures.

**What this works with:**
- Standard library modules (`random`, `json`, `os.path`, `math`)
- Pip packages (`rdkit`, `scipy`, `pandas` — any module with callable functions)
- Your own local modules
- C extensions (inspect-only extraction, no AST)

**How it works under the hood:**
1. Checks `module.__file__` — if it's a `.py` file, runs full AST + inspect dual-pass extraction. If it's a C extension, runs inspect-only.
2. Applies `include`/`exclude` filters.
3. Runs exposure rules (skips private functions, variadics, etc.).
4. Registers into the ToolRegistry with an auto-derived namespace.

### 3. `discover()` — scan an entire directory

Point at a folder. Every public function and class method becomes an MCP tool:

```python
app = SmarterMCP("my-library-server")
app.discover("./src/mylib", exclude=["test_*", "_internal*"])
app.run()
```

**What the extraction engine handles automatically:**
- Module-level functions (annotated and unannotated)
- Instance methods, class methods, static methods
- `async def` functions
- `@property` descriptors → mapped to MCP *resources* (not tools)
- `*args`/`**kwargs` → detected and skipped with a warning
- Inherited methods — controlled by config

**Class instantiation** (for discovered class methods):
1. **Config-defined**: manifest or kwargs declare constructor args
2. **Default constructor**: `cls()` is attempted; if it fails, the class is skipped with a warning

**Namespace routing**: Each source file gets its own namespace. `src/mylib/db.py` → namespace `db`. `src/mylib/ml.py` → namespace `ml`.

### 4. YAML Manifest — zero-code configuration

See [The YAML Manifest](#the-yaml-manifest) section below.

---

## The YAML Manifest

The manifest is a single YAML file that configures everything — sources, exposure rules, instance lifecycles, tool overrides, and server settings. No Python code required.

```yaml
name: chemistry-tools
version: 0.1.0
description: "MCP server for chemistry and utility functions"

server:
  host: 0.0.0.0
  port: 8000
  transport: sse       # sse | streamable-http | stdio

  # Authentication (off by default)
  auth_enabled: false
  auth_header: X-API-Key                 # custom header for HTTP routes
  auth_keys_env: SMARTER_MCP_API_KEYS    # env var holding comma-separated keys

  # Rate limiting (off by default) — sliding window
  rate_limit_enabled: false
  rate_limit_per_minute: 60              # per-session limit
  rate_limit_global_per_minute: 1000     # global limit across all sessions

sources:
  # Scan a local directory
  - path: ./src/my_utils
    exclude: ["test_*", "_internal*"]

  # Import and expose a pip package
  - module: random
    include: [choices, randint, sample]
    namespace: random_tools

  # Another module
  - module: json
    include: [dumps, loads]
    namespace: json_tools

# What gets exposed
expose:
  include_private: false      # skip _private functions
  include_dunder: false       # skip __dunder__ methods
  include_inherited: false    # skip inherited methods
  include_properties: true    # map @property → MCP resources
  variadic_policy: warn       # skip | warn | expose
  unannotated_policy: expose  # expose | warn | skip

# How to instantiate discovered classes
instances:
  - class_name: my_utils.DatabaseClient
    lifecycle: session
    constructor_args:
      host: "${DB_HOST:localhost}"    # env var with default
      port: 5432

# Per-tool overrides
tools:
  - function: random.choices
    name: random_pick
    description: "Pick k random items from a list"
    tests:
      - params: {population: [1,2,3,4,5], k: 2}
        expect_type: list

  - function: my_utils._internal_helper
    expose: false    # explicitly exclude

# Multimodal settings
multimodal:
  auto_detect: true

# LLM-assisted description generation (off by default)
llm:
  enabled: false
  provider: openrouter          # openai | openrouter | anthropic/claude
  model: google/gemini-2.0-flash-001
  # api_key_env: OPENROUTER_API_KEY   # defaults per-provider if omitted
  cache_path: .smarter-mcp/description-cache.json
  overwrite_existing: false     # only fill missing descriptions by default
```

**Environment variable substitution:** Use `${VAR_NAME}` or `${VAR_NAME:default}` anywhere in string values. Smarter-MCP resolves them at load time.

### Scaffold a manifest from an existing codebase

```bash
smarter-mcp init ./my_project
```

This scans the directory, discovers all tools, and generates a commented-out `smarter-mcp.yaml` with everything pre-populated.

---

## The CLI

The `smarter-mcp` command is installed as a console script. All commands accept a target (`.py` file, directory, or manifest).

### `smarter-mcp serve`

Start the MCP server:

```bash
# From a Python script (auto-detects your SmarterMCP instance)
smarter-mcp serve ./server.py

# From a directory (auto-discovers all tools)
smarter-mcp serve ./src/mylib

# From a manifest
smarter-mcp serve --manifest smarter-mcp.yaml

# With overrides
smarter-mcp serve ./server.py --port 3000 --transport streamable-http

# Dev mode — auto-restarts on file changes
smarter-mcp serve ./server.py --dev
```

**Auto-detection logic** (when you pass a `.py` file):
1. Imports the file as a module
2. Looks for conventional variable names: `app`, `server`, `mcp`, `smarter_mcp`
3. Falls back to scanning all module-level variables for a `SmarterMCP` instance
4. If found, calls `.run()` on it

### `smarter-mcp validate`

Dry-run validation — shows what would be exposed without starting a server:

```bash
smarter-mcp validate ./server.py
```

Output includes:
- Server name, transport, host:port
- Each namespace with tool and resource counts
- Per-tool: name, description, parameter count, return type
- Warnings for unannotated parameters, variadics, etc.

### `smarter-mcp test`

Run predefined tool tests:

```bash
# Test all tools
smarter-mcp test ./server.py

# Test a specific tool
smarter-mcp test ./server.py --tool greet

# Test with ad-hoc params
smarter-mcp test ./server.py --tool greet --params '{"name": "Alice"}'
```

Exit code is `0` if all pass, `1` if any fail. Use this in CI/CD.

### `smarter-mcp init`

Scaffold a manifest:

```bash
smarter-mcp init ./my_project        # creates my_project/smarter-mcp.yaml
smarter-mcp init ./my_project --force # overwrite existing
```

### `smarter-mcp export`

*(Coming soon)* — Export as a standalone pip-installable package.

---

## Multimodal Support

Smarter-MCP handles image content in both directions — zero configuration required.

### Returning images

If your tool returns a `PIL.Image.Image`, `np.ndarray`, `bytes`, or `pathlib.Path` pointing to an image, Smarter-MCP automatically encodes it as base64 and wraps it in an MCP `ImageContent` response.

```python
from PIL import Image
from smarter_mcp import tool

@tool()
def rotate_image(img: Image.Image, degrees: int = 90) -> Image.Image:
    """Rotate an image by the specified degrees."""
    return img.rotate(degrees)
```

The return value is intercepted and converted to `ImageContent` before the agent sees it.

### Accepting images

If your parameter has a type annotation of `PIL.Image.Image` or `np.ndarray`, Smarter-MCP:
1. **Rewrites the signature** so FastMCP/Pydantic sees it as `str` (avoiding validation errors)
2. **At call time**, intercepts the string input and resolves it:
   - Local file path → opens and loads the image
   - Remote URL → downloads and loads
   - `data:image/png;base64,...` data URL → decodes
   - Raw base64 string → decodes
   - MCP `ImageContent` dict → extracts the data

```python
import numpy as np
from smarter_mcp import tool

@tool()
def detect_faces(img: np.ndarray, min_confidence: float = 0.8) -> list[dict]:
    """Detect faces in an image array."""
    # img is already a numpy array — Smarter-MCP decoded it for you
    ...
```

### Lazy dependencies

`Pillow` and `numpy` are **not** imported at startup. They're only loaded when a tool that uses them is actually called. If they're not installed and no tool needs them, everything works fine. If a tool *does* need them, a clear error tells you to run `pip install smarter-mcp[multimodal]`.

---

## The Tool Testing Framework

Every tool can have test cases. Smarter-MCP runs them to verify tools are alive, callable, and returning expected results — before an agent ever touches them.

### Defining tests

**In decorators:**

```python
from smarter_mcp import tool

@tool(
    tests=[
        {"params": {"name": "Alice"}, "expect": "Hello, Alice!"},
        {"params": {"name": "Bob"}, "expect": "Hello, Bob!"},
    ]
)
def greet(name: str) -> str:
    return f"Hello, {name}!"
```

**In YAML manifests:**

```yaml
tools:
  - function: greet
    tests:
      - params: {name: "Alice"}
        expect: "Hello, Alice!"
      - params: {name: "Bob"}
        expect_type: str    # just verify it returns a string
```

### What each test checks

| Check | What it verifies |
|---|---|
| **Schema** | JSON schema builds correctly |
| **Callable** | Function is actually callable |
| **Instance** | For class methods — the class can be instantiated |
| **Execution** | Call completes without raising |
| **Return type** | Matches `expect_type` if specified |
| **Return value** | Equals `expect` if specified |
| **Serializable** | Return value can be JSON-serialized |

### Running tests programmatically

```python
# All tools
report = app.test()
print(report.summary())  # "Results: 5 passed, 1 failed, 2 skipped"

# Specific tool
report = app.test("greet")

# Ad-hoc test with custom params
report = app.test("greet", params={"name": "Charlie"})
```

### Running tests from the CLI

```bash
smarter-mcp test ./server.py --tool greet
```

---

## Structured Error Handling

When a tool call fails, Smarter-MCP returns a **structured error object** instead of leaking a raw Python traceback to the agent. This gives the model something it can actually reason about.

Every tool is wrapped so that exceptions are caught and serialized as JSON:

```json
{
  "error": true,
  "error_type": "coercion_error",
  "tool": "add",
  "message": "Cannot coerce 'abc' to 'int' for parameter 'a': ...",
  "details": "..."
}
```

Two error types are distinguished:

| `error_type` | When it happens | Meaning for the agent |
|---|---|---|
| `coercion_error` | An argument can't be coerced to the expected type (bad input) | Fix the arguments and retry |
| `execution_error` | The tool function itself raised | Internal failure — retrying the same call won't help |

The error types live in `smarter_mcp/errors.py` (`SmarterMCPError`, `CoercionError`, `ToolExecutionError`) and `format_error_response()` produces the payload. Coercion failures are logged at `warning` level; execution failures at `error` with a full traceback in the logs — but the agent only ever sees the clean structured object.

---

## HTTP Endpoints & Introspection

Beyond the MCP protocol itself, the server exposes two plain HTTP endpoints for operational monitoring and schema introspection.

### `GET /health`

Returns server status, namespaces, and counts — useful for load balancers and uptime checks:

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "ok",
  "name": "my-server",
  "namespaces": ["default", "random"],
  "tool_count": 7,
  "resource_count": 1
}
```

`/health` is always **exempt from authentication** so health checks keep working even when auth is enabled.

### `GET /mcp/{namespace}/schema`

Returns an OpenAPI 3.1-compatible JSON schema for every tool in a namespace:

```bash
curl http://localhost:8000/mcp/default/schema
```

**Compact mode** — when a namespace has many tools or long docs, the full schema can get large. Pass `?compact=true` to get just tool names and parameter names:

```bash
curl "http://localhost:8000/mcp/default/schema?compact=true"
```

```json
{
  "namespace": "default",
  "tools": [
    {"name": "add", "params": ["a", "b"]},
    {"name": "greet", "params": ["name"]}
  ]
}
```

An unknown namespace returns `{"error": "..."}`.

### Getting the ASGI app

For custom deployments or ASGI test clients, `app.http_app()` returns the Starlette app with auth middleware already applied:

```python
app = SmarterMCP("my-server")
# ... register tools ...
asgi = app.http_app()   # build() is called automatically if needed
```

---

## Authentication & Rate Limiting

Both are **off by default** and driven entirely by the `server` block of the manifest (or `app._config.server`).

### Authentication (two layers, same key set)

When `auth_enabled: true`, Smarter-MCP enforces auth two ways from one set of keys:

1. **Custom `X-API-Key` header** (ASGI middleware) — protects the HTTP routes (`/mcp/...`, `/mcp/{ns}/schema`). `/health` is exempt. Missing/invalid keys get a `401` JSON response.
2. **FastMCP-native Bearer auth** — a `StaticTokenVerifier` is wired into the root server so MCP-protocol clients can authenticate with `Authorization: Bearer <key>`.

Keys are read from the env var named by `auth_keys_env` (default `SMARTER_MCP_API_KEYS`), comma-separated:

```bash
export SMARTER_MCP_API_KEYS="key-abc,key-def"
```

```bash
# Rejected — no key
curl -i http://localhost:8000/mcp/default/schema          # 401

# Accepted
curl -i -H "X-API-Key: key-abc" http://localhost:8000/mcp/default/schema   # 200

# Health is always open
curl -i http://localhost:8000/health                       # 200
```

> Note: FastMCP's `StaticTokenVerifier` stores tokens in memory and is not intended as a hardened production secret store — keep your key env var secured.

### Rate Limiting (sliding window)

When `rate_limit_enabled: true`, two sliding-window limiters are attached as MCP middleware:

- **Per-session**: `rate_limit_per_minute` (default 60) — keyed by MCP session id.
- **Global**: `rate_limit_global_per_minute` (default 1000) — across all sessions.

Requests over a threshold raise a rate-limit error back to the client. Because the middleware attaches to the server object, limits apply to in-memory `fastmcp.Client` connections too — which makes them deterministically testable.

The security construction lives in `smarter_mcp/server/security.py` (`load_api_keys`, `APIKeyMiddleware`, `build_auth_provider`, `build_rate_limit_middleware`).

---

## LLM Description Generation

Auto-discovered code often lacks docstrings. When `llm.enabled: true`, Smarter-MCP uses an LLM to generate a concise, one-sentence description for every tool that's missing one — so agents get useful context even for legacy code.

```yaml
llm:
  enabled: true
  provider: openrouter            # openai | openrouter | anthropic/claude
  model: google/gemini-2.0-flash-001
  overwrite_existing: false       # only fill blanks (set true to regenerate all)
```

### Provider support (OpenAI SDK)

v1 uses the **OpenAI Python SDK** as the single backend. The `provider` field selects the endpoint and API-key env var:

| `provider` | Endpoint | API-key env (default) |
|---|---|---|
| `openai` | OpenAI default | `OPENAI_API_KEY` |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `anthropic` / `claude` | `https://api.anthropic.com/v1/` (OpenAI-compat) | `ANTHROPIC_API_KEY` |

Claude is reachable either through **OpenRouter** (recommended) or Anthropic's OpenAI-compatible endpoint. `base_url` and `api_key_env` can be overridden explicitly. Install the extra with `pip install smarter-mcp[llm]`.

### Caching

Generated descriptions are cached on disk at `cache_path` (default `.smarter-mcp/description-cache.json`), keyed by a hash of each tool's signature + existing docstring. Re-running over an unchanged codebase makes **zero** LLM calls.

### Resilient by design

Description generation runs during `build()`, after discovery and before the server is assembled. If the API key or `openai` package is missing, it logs a warning and the server **still builds** — LLM enrichment never blocks startup. If nothing needs a description, no client is constructed and no key is required.

---

## Architecture Deep Dive

### The Pipeline

Every tool — regardless of how it was registered — passes through the same runtime pipeline:

```
Registration:
  @tool() ──────────────────────┐
  @toolkit() ───────────────────┤
  app.discover_module() ────────┤──→ ToolRegistry
  app.discover() ───────────────┤
  smarter-mcp.yaml ─────────────┘

Build:
  ToolRegistry ──→ NamespaceRouter ──→ FastMCP (with mounted sub-servers)

Per-call runtime:
  Agent request
    → Schema validation (are the params valid?)
    → Type coercion ("42" → 42, stringified JSON → dict)
    → Multimodal input interception (base64/URL → PIL.Image)
    → Instance resolution (for class methods: resolve self)
    → Context injection (if function accepts FastMCP Context)
    → Execute the function
    → Multimodal output interception (PIL.Image → ImageContent)
    → Return to agent
```

### Type Coercion

LLMs frequently send values as strings even when the schema says `integer`. Smarter-MCP silently handles common cases:

| Input | Expected type | Coerced to |
|---|---|---|
| `"42"` | `int` | `42` |
| `"3.14"` | `float` | `3.14` |
| `"true"` | `bool` | `True` |
| `'{"key": "val"}'` | `dict` | `{"key": "val"}` |
| `"[1, 2, 3]"` | `list` | `[1, 2, 3]` |

When coercion is ambiguous, the raw value is passed through with a warning logged.

### Namespace Routing

Each source gets its own FastMCP sub-server, mounted on a root:

```
decorator tools     → localhost:8000/mcp/default
random (module)     → localhost:8000/mcp/random
./src/mylib/db.py   → localhost:8000/mcp/db
./src/mylib/ml.py   → localhost:8000/mcp/ml
```

The root at `localhost:8000/mcp` aggregates all tools. Agents can connect to a specific namespace or the root.

### Docstring Parsing

Smarter-MCP parses three docstring formats to extract per-parameter descriptions:

**Google style:**
```python
def func(name: str, age: int) -> str:
    """Greet a user.

    Args:
        name: The user's name.
        age: The user's age in years.
    """
```

**NumPy style:**
```python
def func(name: str, age: int) -> str:
    """Greet a user.

    Parameters
    ----------
    name : str
        The user's name.
    age : int
        The user's age in years.
    """
```

**Sphinx style:**
```python
def func(name: str, age: int) -> str:
    """Greet a user.

    :param name: The user's name.
    :param age: The user's age in years.
    """
```

These descriptions flow directly into the JSON schema that the agent sees, giving it better context for how to call your tools.

---

## Using Smarter-MCP in Another Project

If you're developing Smarter-MCP locally and want to test it in a separate project:

### 1. Install from local source

In your other project's virtual environment:

```bash
cd /path/to/your/other/project
source .venv/bin/activate

# Install smarter-mcp from your local checkout (editable)
pip install -e "/path/to/pymcp[all]"
```

The `-e` flag means changes to the smarter-mcp source take effect immediately — no re-install needed.

### 2. Use it

```python
# In your other project
from smarter_mcp import SmarterMCP
import my_project_module

app = SmarterMCP("my-project-server")
app.discover_module(my_project_module)
app.run()
```

Or from the CLI:

```bash
smarter-mcp serve ./src --port 8000
smarter-mcp validate ./src
```

---

## What's Implemented vs. What's Coming

### ✅ Implemented and Working

- **Decorator API**: `@tool()`, `@toolkit()`, `@resource()` (standalone, importable from anywhere) with full lifecycle management
- **Module discovery**: `app.discover_module()` for any importable Python module
- **Directory discovery**: `app.discover()` with AST + inspect dual-pass extraction
- **CLI**: `serve`, `validate`, `test`, `init` commands with auto-detection
- **Hot-reload dev mode**: `smarter-mcp serve --dev` (via watchfiles)
- **Multimodal**: PIL.Image / numpy.ndarray input decoding and output encoding
- **Tool testing**: Decorator-defined and YAML-defined test cases, CLI runner
- **Type coercion**: String → int/float/bool/dict/list/image
- **Docstring parsing**: Google, NumPy, Sphinx formats
- **Namespace routing**: Auto-derived from module paths with FastMCP mount
- **YAML manifests**: Full config with env var substitution
- **Manifest scaffolding**: `smarter-mcp init` generates commented YAML
- **Structured error handling**: Structured MCP error objects (coercion vs. execution) instead of raw tracebacks
- **HTTP endpoints**: `GET /health` and `GET /mcp/{namespace}/schema` (with `?compact=true`)
- **Server auth**: API key (`X-API-Key` middleware + FastMCP Bearer `StaticTokenVerifier`)
- **Rate limiting**: Sliding window, per-session and global
- **LLM description generation**: Auto-generate tool descriptions via the OpenAI SDK (OpenAI / OpenRouter / Anthropic), with on-disk caching

### 🔜 Coming Soon

- **Package export**: `smarter-mcp export` → standalone pip-installable package
- **LLM v2**: Native multi-provider via LiteLLM (param-level descriptions, batch generation)
