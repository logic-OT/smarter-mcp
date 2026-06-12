# Smarter-MCP — Specification

> **Historical design spec** — reflects intended architecture at time of writing; always verify against current source for exact field names and behavior.

> The highest-level Python framework for building and generating MCP servers.

---

## Philosophy

FastMCP gives you primitives. Smarter-MCP gives you **abstractions**.

The difference is the same as PyTorch vs Keras, or LangChain vs Agno. Smarter-MCP sits on top of FastMCP and handles everything the developer shouldn't have to think about: signature extraction, type coercion, instance lifecycle, namespace routing, multimodal encoding, and structured error handling.

**Two core principles:**

1. **If you own the code** — write clean, decorated Python. Smarter-MCP gives you `@tool`, `@toolkit`, and `@resource` standalone decorators that plug into a powerful runtime with session-scoped instances, automatic type coercion, and multimodal support.

2. **If you don't own the code** — point Smarter-MCP at it. Pass an imported module object, a directory path, or a YAML manifest. The dual-pass extraction engine (AST + inspect) handles the rest — no source modifications required.

Both paths converge into the same runtime engine. Every tool gets the same production-grade treatment regardless of how it was registered.

---

## The Four Entry Points

Smarter-MCP provides four ways to register MCP tools. All four funnel into a single `ToolRegistry`, which feeds the runtime pipeline and server.

### 1. Decorators — for writing new tools

When you're authoring MCP tools from scratch, decorators give you the cleanest possible API.

**Simple function tools:**

```python
from smarter_mcp import SmarterMCP, tool

app = SmarterMCP("my-server")

@tool("Greet a user by name")
def greet(name: str) -> str:
    return f"Hello, {name}!"

@tool()  # description auto-extracted from docstring
async def fetch_data(url: str, timeout: int = 30) -> dict:
    """Fetch JSON data from a URL."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=timeout)
        return resp.json()

app.run()
```

**Class-based toolkits** — for stateful tools (DB clients, API wrappers, ML pipelines):

```python
from smarter_mcp import tool, toolkit

@toolkit(lifecycle="session")
class DatabaseClient:
    def __init__(self, host: str = "localhost", port: int = 5432):
        self.conn = connect(host, port)

    @tool(name="run_query")
    def query(self, sql: str) -> list[dict]:
        """Execute a SQL query and return results."""
        return self.conn.execute(sql).fetchall()

    @tool()
    def list_tables(self) -> list[str]:
        """List all tables in the database."""
        return self.conn.get_tables()
```

The `@toolkit()` decorator handles:
- Automatic class instantiation with the specified lifecycle (session, singleton, or per-call)
- `self` binding on every tool call — each MCP session gets its own instance
- Constructor argument injection from decorator kwargs or environment variables
- Session instances evicted via bounded LRU (max 256 entries) with best-effort cleanup (`close()`/`__exit__`) on eviction

**Resources:**

```python
from smarter_mcp import resource

@resource("config://settings")
def get_settings() -> dict:
    return {"debug": True, "version": "1.0"}
```

---

### 2. `discover_module()` — expose any imported Python module

The killer feature. Import any module — stdlib, pip package, or local file — and hand it to Smarter-MCP. It extracts the functions, builds schemas, and serves them as MCP tools.

```python
import random
import json
from rdkit.Chem import Descriptors
import my_local_utils

app = SmarterMCP("multi-tools")

# Stdlib
app.discover_module(random, include=["choices", "randint", "sample"])
app.discover_module(json, include=["dumps", "loads"])

# Pip packages
app.discover_module(
    Descriptors,
    include=["MolWt", "MolLogP", "NumHDonors"],
    namespace="chemistry",
)

# Local module (expose everything)
app.discover_module(my_local_utils)

app.run()
```

**How it works under the hood:**

1. Receives the actual module object (already imported by the developer)
2. Checks `module.__file__` to determine extraction strategy:
   - `.py` source file → full AST + inspect dual-pass extraction
   - `.so` / `.pyd` / built-in → inspect-only extraction (C extensions)
3. Applies `include` / `exclude` filter to scope what gets exposed
4. Runs exposure rules (variadic policy, unannotated policy, private filtering)
5. Registers everything into the `ToolRegistry`

**What this unlocks:** Any Python package ever published on PyPI becomes an MCP server in one line of code. Stdlib utilities, data science libraries, chemistry toolkits, anything.

---

### 3. `discover()` — scan a source directory

For scanning an entire local codebase. This is the original zero-touch auto-discovery mode.

```python
app = SmarterMCP("my-library-server")

# Scan a directory
app.discover("./src/mylib", exclude=["test_*", "_internal*"])

# Mix with decorator tools on top
from smarter_mcp import tool

@tool("Custom scoring function")
def custom_score(data: dict) -> float:
    ...

app.run()
```

**What the extraction engine handles out of the box, with zero source modification:**

- Module-level functions (annotated and unannotated)
- Instance methods, with automatic class instantiation
- Class methods (`@classmethod`)
- Static methods (`@staticmethod`)
- `@property` descriptors — exposed as MCP *resources*, not tools
- `async def` functions and async methods
- Functions with `*args` / `**kwargs` — detected and excluded by default with a warning
- Dataclass methods
- Inherited methods — controlled by `include_inherited: true/false` in config

**Class instantiation model** (for auto-discovered classes):

When a class method is exposed via auto-discovery, Smarter-MCP needs an instance to call it on. Three strategies, in order of precedence:

1. **Config-defined** (explicit): the manifest or `discover()` kwargs declare constructor args
2. **Singleton factory** (convention): a module-level function named `get_<ClassName>()` or `create_<ClassName>()` is detected and used automatically
3. **Default constructor** (fallback): `cls()` is attempted; if it fails, the class is surfaced as a warning and skipped

Instance lifecycle is session-scoped by default: one instance per MCP session, evicted via bounded LRU (max 256 entries) with best-effort cleanup (`close()`/`__exit__`) on eviction (FastMCP 3.3.1 exposes no session-disconnect hook). Stateful classes (database clients, connection pools) work correctly — each agent session gets its own instance.

**Name collision resolution:**

When two classes expose a method with the same name, Smarter-MCP defaults to `ClassName_method_name` namespacing. Configurable per-tool in the manifest.

**Property → Resource mapping:**

`@property` descriptors with a return type annotation are automatically mapped to MCP resources with URI pattern `resource://{module}/{ClassName}/{property_name}`.

---

### 4. CLI — zero-code, YAML-driven

For operational use — no Python code needed. Configure everything in `smarter-mcp.yaml`.

**Commands:**

```bash
# Auto-discover a local directory
smarter-mcp serve ./mylib

# Use a manifest
smarter-mcp serve --manifest smarter-mcp.yaml

# Run a decorator-based Python file directly
smarter-mcp serve ./server.py

# Validate manifest without starting the server
smarter-mcp validate

# Generate a starter manifest from a codebase
smarter-mcp init ./mylib

# Export as standalone pip-installable package
smarter-mcp export --output ./dist --package-name mylib-mcp
```

**YAML Manifest:**

```yaml
name: chemistry-tools
version: 0.1.0
description: "MCP server for chemistry and utility functions"

server:
  host: 0.0.0.0
  port: 8000
  transport: sse       # sse | streamable-http | stdio

sources:
  # Local codebase
  - path: ./src/my_local_utils
    exclude:
      - "test_*"
      - "_internal*"

  # Imported modules
  - module: random
    include: [choices, randint, sample]
    namespace: random_tools

  - module: json
    include: [dumps, loads]
    namespace: json_tools

  - module: rdkit.Chem.Descriptors
    include: [MolWt, MolLogP, NumHDonors, NumHAcceptors]
    namespace: chemistry

# Global exposure rules
expose:
  include_private: false
  include_inherited: false
  unannotated_policy: warn        # warn | expose | skip
  variadic_policy: skip           # skip | warn | expose
  include_properties: true

# Class instantiation (for path-based discovery)
instances:
  - class_name: my_local_utils.DBClient
    constructor_args:
      host: "${DB_HOST}"     # env var substitution
      port: 5432
    lifecycle: session

  - class_name: my_local_utils.Pipeline
    factory: my_local_utils.build_default_pipeline
    lifecycle: singleton

# Per-tool overrides (rename, redescribe, exclude)
tools:
  - function: random.choices
    name: random_pick
    description: "Pick k random items from a list, with optional weights"

  - function: json.dumps
    name: to_json
    description: "Convert a Python dict/list to a JSON string"

  - function: my_local_utils._internal_helper
    expose: false            # explicitly exclude

# Multimodal return type detection
multimodal:
  auto_detect: true

# LLM-assisted description generation
llm:
  enabled: false
  provider: openrouter
  model: google/gemini-2.0-flash-001
  cache_path: .smarter-mcp/description-cache.json
```

**CLI auto-detection:** When `smarter-mcp serve ./server.py` is called and the file contains a `SmarterMCP` instance, the CLI imports the file and runs that instance directly — supporting decorator-based servers from the command line.

---

## Architecture: The ToolRegistry

The `ToolRegistry` is the unification point. All four entry points write into it. The server layer reads from it.

```
                    ┌─ @tool ──────────────────────┐
                    │  @resource ──────────────────┤
Developer Code ─────┤  @toolkit ───────────────────┤──→ ToolRegistry ──→ Router ──→ FastMCP
                    └──────────────────────────────┘
                                                     ↑
app.discover_module(mod) ──→ Inspect Extract ────────┤
                                                     │
app.discover("./path") ────→ AST + Inspect Extract ──┤
                                                     │
smarter-mcp.yaml ───────────→ AST + Inspect Extract ──┘
```

**Why this matters:** A developer can mix all four entry points in a single server. Auto-discover a legacy library, add a few hand-written decorator tools, and expose some stdlib utilities — all served from one endpoint. If a conflict arises (same tool name), decorator registration wins with a warning logged.

---

## Namespace Routing

Each source (module, directory, decorator group) gets its own MCP namespace. Namespaces are independently addressable sub-servers mounted on a root.

**Default routing:**

```
random tools       →  localhost:8000/mcp/random_tools
json tools         →  localhost:8000/mcp/json_tools
./src/mylib/db.py  →  localhost:8000/mcp/db
./src/mylib/ml.py  →  localhost:8000/mcp/ml
decorator tools    →  localhost:8000/mcp/default
```

A root endpoint at `localhost:8000/mcp` aggregates all tools from all namespaces for clients that want the full surface.

**Transport:** HTTP with SSE (the MCP standard). stdio also supported for local agent integrations. Both run simultaneously if configured.

**Why this matters:** Agents and orchestrators can be pointed at a specific namespace rather than the full tool surface, reducing tool-selection confusion and making access control easier.

---

## Runtime Engine

Every tool — regardless of how it was registered — passes through the same runtime pipeline before execution:

1. **Schema validation.** Parameters are validated against the JSON schema before the function is called. Clear error messages are returned to the agent, not Python tracebacks.

2. **Type coercion.** Common agent mistakes (sending `"5"` for an `int`, sending stringified JSON for a `dict`) are silently coerced when unambiguous. When ambiguous, a structured error is returned.

3. **Instance resolution.** For class methods: resolves the correct instance (session-scoped, singleton, or per-call) and binds `self`.

4. **Context injection.** If the function accepts a FastMCP `Context` parameter, it's injected automatically.

5. **Multimodal interception.** Return values of type `PIL.Image.Image`, `numpy.ndarray`, `bytes`, or `pathlib.Path` (image extension) are automatically converted to MCP `ImageContent`.

6. **Structured error handling.** All exceptions are caught, logged server-side, and returned as structured MCP error objects — not raw tracebacks.

---

## Multimodal Support

Smarter-MCP detects and correctly handles multimodal content in both directions — functions that *return* images/audio and functions that *accept* them.

### Output Detection (function returns → MCP content)

| Return type | MCP content type |
|---|---|
| `PIL.Image.Image` | `ImageContent` (PNG/JPEG) |
| `bytes` + MIME hint | `BlobContent` |
| `np.ndarray` (2D/3D) | `ImageContent` (auto-encoded) |
| `pathlib.Path` (image ext) | `ImageContent` (read and encode) |
| `str` | `TextContent` |
| `dict` / `list` | `TextContent` (JSON serialized) |

**Detection strategy:**

1. Check return type annotation first (`-> PIL.Image.Image`)
2. Check function name patterns (`get_image_*`, `render_*`, `plot_*`)
3. At call time, inspect the actual return value type as a fallback

### Input Detection (MCP content → function parameters)

This is the harder problem: how does Smarter-MCP know that a function *accepts* multimodal input, especially for auto-discovered modules?

**Detection strategy (in priority order):**

| Signal | Example | How Smarter-MCP uses it |
|---|---|---|
| Type annotation | `image: PIL.Image.Image` | Knows to decode incoming `ImageContent` to a PIL Image |
| Type annotation | `data: np.ndarray` | Decodes `ImageContent` to numpy array |
| Type annotation | `audio: bytes` | Passes raw bytes from `BlobContent` |
| `Annotated` hint | `Annotated[bytes, "image/png"]` | Knows the MIME type to expect |
| Parameter name | `image`, `img`, `photo`, `frame` | Heuristic: likely accepts an image |
| Decorator explicit | `@tool(inputs={"scan": "image"})` | Developer declares it |
| Manifest override | `parameters: scan: {type: image}` | YAML declares it |

**How it works at runtime:**

When an agent sends `ImageContent` (base64-encoded image data) to a tool, the `ToolWrapper` intercepts it and converts it based on the parameter's detected type:

```
ImageContent (base64) ──→ PIL.Image.Image  (if param: PIL.Image.Image)
                      ──→ np.ndarray       (if param: np.ndarray)
                      ──→ bytes            (if param: bytes)
                      ──→ pathlib.Path     (if param: Path — writes to temp file)
```

**Decorator example:**

```python
from smarter_mcp import tool

@tool()
def classify_image(image: PIL.Image.Image, top_k: int = 5) -> list[str]:
    """Classify an image and return top-k labels."""
    ...
```

**Auto-discovered example** (no source modification):

```python
# In some library you don't own:
def detect_faces(img: np.ndarray, min_confidence: float = 0.8) -> list[dict]:
    """Detect faces in an image array."""
    ...
```

Smarter-MCP sees `img: np.ndarray`, recognizes this as a multimodal input, and automatically wires up the `ImageContent` → `np.ndarray` conversion. The agent can send an image and it just works.

**When auto-detection fails** (e.g., `data: bytes` with no annotation hint), the parameter is treated as a regular string/base64. The developer can override this in the manifest or with decorator kwargs.

### Audio support

`bytes` parameters annotated as `audio/wav` or `audio/mp3` (via `Annotated[bytes, "audio/wav"]`) are surfaced as audio-accepting tool parameters. Return values similarly.

---

## Lightweight Core

Smarter-MCP's core is intentionally lean. Heavy dependencies are optional extras — you only install what you use.

**Core install** (`pip install smarter-mcp`):

| Dependency | Size | Why |
|---|---|---|
| `fastmcp>=3.0` | ~2MB (pulls `mcp`, `starlette`, `uvicorn`) | MCP protocol + server |
| `pydantic>=2.0` | ~3MB | Schema validation (already required by fastmcp) |
| `click>=8.0` | ~200KB | CLI |
| `pyyaml>=6.0` | ~200KB | Manifest parsing |
| **Total** | **~5.5MB** | |

**Optional extras:**

```bash
pip install smarter-mcp[multimodal]   # +Pillow (~60MB), +numpy (~30MB)
pip install smarter-mcp[llm]          # +openai (~1MB)
pip install smarter-mcp[all]          # everything
```

**Lazy imports:** Smarter-MCP never imports `PIL`, `numpy`, or `openai` at module load time. These are imported only when a tool that needs them is actually registered or called. If `Pillow` isn't installed and no tool uses images, nothing breaks. If a tool *does* return a `PIL.Image` but Pillow isn't installed, a clear error tells the developer to `pip install smarter-mcp[multimodal]`.

---

## Production-Grade Runtime

**Request tracing.** Every tool call is logged with: timestamp, tool name, namespace, session ID, parameter summary (truncated), latency, and result status. Structured JSON logs.

**Health endpoint.** `GET /health` returns server status, uptime, loaded namespaces, and tool counts.

**Schema introspection.** `GET /mcp/{namespace}/schema` returns the full OpenAPI-compatible JSON schema for all tools in that namespace.

**Concurrency.** The server is async-native (asyncio). Sync functions are run in a thread pool executor.

**Graceful shutdown.** SIGTERM triggers session cleanup (instance teardown), in-flight request completion, then shutdown.

**Rate limiting (optional):**

```yaml
server:
  rate_limit_enabled: true
  rate_limit_per_minute: 100
  rate_limit_global_per_minute: 1000
```

**Auth (optional):**

```yaml
server:
  auth_enabled: true
  auth_header: X-API-Key
  auth_keys_env: MCP_API_KEYS
```

---

## Exportable Package

Any configuration that produces a working server can be exported as a standalone pip-installable Python package.

```bash
smarter-mcp export --output ./dist --package-name mylib-mcp --version 0.1.0
```

**The exported package:**

```
mylib-mcp/
  pyproject.toml
  README.md                  # auto-generated, lists all exposed tools
  src/
    mylib_mcp/
      __init__.py
      server.py              # main entrypoint
      namespaces/
        utils.py
        db/
          client.py
      _instance_factory.py
      _type_adapters.py
      _manifest.yaml         # embedded config
```

- Has no dependency on Smarter-MCP itself at runtime (only `mcp` and `fastmcp`)
- Preserves all instance lifecycle logic
- Is importable as a library or runnable as `python -m mylib_mcp`

---

## Tool Testing

Every registered tool can have test cases defined alongside it. Smarter-MCP runs them to verify tools are alive, callable, and returning expected results — before an agent ever touches them.

### Defining test params with decorators

```python
from smarter_mcp import tool, toolkit

@tool(
    description="Greet a user",
    tests=[
        {"params": {"name": "Alice"}, "expect": "Hello, Alice!"},
        {"params": {"name": "Bob"}},  # just verify it doesn't crash
    ]
)
def greet(name: str) -> str:
    return f"Hello, {name}!"

@toolkit(lifecycle="session")
class DatabaseClient:
    def __init__(self, host: str = "localhost"):
        self.conn = connect(host)

    @tool(
        tests=[{"params": {"sql": "SELECT 1"}}]
    )
    def query(self, sql: str) -> list[dict]:
        """Execute a SQL query."""
        return self.conn.execute(sql).fetchall()
```

### Defining test params in the manifest

```yaml
tools:
  - function: random.choices
    name: random_pick
    description: "Pick k random items from a list"
    tests:
      - params:
          population: [1, 2, 3, 4, 5]
          k: 2
        expect_type: list     # verify return type

  - function: json.dumps
    name: to_json
    tests:
      - params:
          obj: {"key": "value"}
        expect: '{"key": "value"}'
```

### Running tests programmatically

```python
# Test a specific tool with params
result = app.test("greet", params={"name": "Alice"})
# → TestResult(passed=True, output="Hello, Alice!", latency_ms=2)

# Test a specific tool using its predefined test cases
result = app.test("greet")
# → runs all tests defined in the decorator

# Test all tools
results = app.test()
# → runs every tool's test cases, reports pass/fail

# Verbose output
results = app.test(verbose=True)
```

### CLI

```bash
# Test all tools (runs predefined test cases)
smarter-mcp test

# Test a specific tool
smarter-mcp test --tool greet

# Test with custom params from the command line
smarter-mcp test --tool greet --params '{"name": "Alice"}'

# Verbose output (show return values, latency)
smarter-mcp test --verbose
```

### What each test checks

| Check | What it verifies |
|---|---|
| **Schema** | JSON schema for the tool is valid and well-formed |
| **Callable** | Function can be resolved and is actually callable |
| **Instance** | For class methods, the class can be instantiated |
| **Execution** | Call with the provided params completes without error |
| **Return type** | Return value matches `expect_type` if specified |
| **Return value** | Return value equals `expect` if specified |
| **Serializable** | Return value can be serialized to MCP content |

### Example output

```
$ smarter-mcp test --verbose

Testing 8 tools across 3 namespaces...

  random_tools/
    ✓ random_pick       params={population: [1,2,3,4,5], k: 2}  → [3, 1]  (1ms)
    ✓ random_int        params={a: 1, b: 100}                   → 42      (0ms)

  json_tools/
    ✓ to_json           params={obj: {"key": "value"}}           → '{"key": "value"}'  (0ms)

  default/
    ✓ greet             params={name: "Alice"}                   → "Hello, Alice!"  (0ms)
    ✓ greet             params={name: "Bob"}                     → "Hello, Bob!"    (0ms)
    ✓ run_query         params={sql: "SELECT 1"}                 → [{"1": 1}]       (12ms)
    ✗ broken_tool       params={x: 1}                            → TypeError: ...    (1ms)
    ○ no_tests_defined  (skipped — no test cases defined)

Results: 6 passed, 1 failed, 1 skipped
```

**Why this matters:** You can run `smarter-mcp test` in CI/CD. When an upstream library updates and breaks a function signature, your MCP server tests catch it before deployment. No other MCP framework has this.

---

## Additional Features

**Hot reload in dev mode.** `smarter-mcp serve --dev` watches source files and reloads tools without restarting the server.

**Dry-run mode.** `smarter-mcp validate` checks the manifest, resolves all functions, reports what would be exposed and what would be skipped, and why. No server starts.

**Type inference for unannotated code.** For functions without type annotations, Smarter-MCP infers types from default values, return statements, and docstring type hints (NumPy/Google/Sphinx style). Inferred types are marked as such in the schema.

**Docstring extraction.** NumPy-style, Google-style, Sphinx-style, and plain docstrings are all parsed to extract per-parameter descriptions.

**`smarter-mcp init` scaffold.** Inspects a codebase and generates a starting manifest with all detected functions, classes, and suggested configuration.

---

## What Smarter-MCP Is Not

- **Not multi-language.** Python only for MVP.
- **Not a managed service.** Smarter-MCP produces a server you run.
- **Not a replacement for FastMCP.** Smarter-MCP is built on top of FastMCP. It adds the abstractions — extraction, lifecycle, coercion, routing — that FastMCP leaves to the developer.

---

## Gap Summary vs FastMCP

| Capability | FastMCP | Smarter-MCP |
|---|---|---|
| Decorator API | `@mcp.tool()` (manual wiring) | `@tool` with auto coercion, schema, multimodal |
| Class toolkits | Manual `add_tool()` per method | `@toolkit` with session-scoped instances |
| Expose existing modules | Not supported | `app.discover_module(random)` |
| Auto-discover codebase | Not supported | `app.discover("./mylib")` |
| CLI / YAML config | Not supported | `smarter-mcp serve`, full manifest |
| Instance lifecycle | Manual | session / singleton / per-call, auto-managed |
| Type coercion | None | `"42"` → `42` automatically |
| Namespace routing | Manual `mount()` | Auto-derived from module paths |
| Multimodal | Manual encoding | PIL.Image, ndarray, bytes auto-detected |
| Tool name collisions | Silent overwrite | Auto-namespaced (`ClassName_method`) |
| Property → Resource | Not supported | Automatic mapping |
| Docstring parsing | Raw docstring only | Google/NumPy/Sphinx parsed per-parameter |
| Type inference | None | AST-based inference from defaults + returns |
| Error handling | Raw Python tracebacks | Structured MCP error objects |
| Package export | Not supported | `smarter-mcp export` → standalone package |
| `*args/**kwargs` | Silent runtime failure | Detected, skipped with warning |
| Schema validation | None | Every call validated against JSON schema |

---

## Requirements

- Python 3.10+
- FastMCP 3.0+
