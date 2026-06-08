# Smarter-MCP 🚀

> **The highest-level Python framework for building, generating, and running MCP servers.**

Smarter-MCP sits on top of `FastMCP` and acts as the orchestration layer that handles everything a developer shouldn't have to think about: AST-based signature extraction, type coercion, class-instance lifecycle management, namespace routing, multimodal content interception, and structured error handling.

---

## 📖 Philosophy: Primitives vs. Abstractions

FastMCP gives you **primitives**. Smarter-MCP gives you **abstractions**.

1. **If you own the code**: Write clean, decorated Python. Smarter-MCP provides `@tool`, `@toolkit`, and `@resource` standalone decorators that plug into a powerful runtime featuring session-scoped instances, automatic type coercion, and multimodal support.
2. **If you don't own the code**: Point Smarter-MCP at it. Pass an imported module object, a directory path, or a YAML manifest. The dual-pass extraction engine (AST + inspect) handles the rest—no source modifications required.

Both paths converge into the same runtime engine. Every tool gets the same production-grade treatment regardless of how it was registered.

### 1. Decorators (For Writing New Tools)

Decorators provide a clean, native Python API for authoring MCP servers from scratch.

* **Simple function tools & resources:**
```python
from smarter_mcp import SmarterMCP

app = SmarterMCP("my-server")

from smarter_mcp import tool, resource

@tool("Greet a user by name")
def greet(name: str) -> str:
    return f"Hello, {name}!"

@resource("config://settings")
def get_settings() -> dict:
    return {"debug": True, "version": "1.0"}
```

* **Class-based toolkits (For DB clients, stateful APIs, ML pipelines):**
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
```
The `@toolkit()` decorator automatically manages session instantiations, `self` binding per session, constructor argument injections, and cleanup on session termination.

---

### 2. `discover_module()` (Expose Any Python Module)

Import any standard library module, third-party pip package, or local file and hand it to Smarter-MCP. It automatically extracts methods, builds schemas, and serves them.

```python
import random
import json
from smarter_mcp import SmarterMCP

app = SmarterMCP("multi-tools")

# Instantly expose safe subsets of libraries
app.discover_module(random, include=["choices", "randint", "sample"])
app.discover_module(json, include=["dumps", "loads"])
app.run()
```
**What this unlocks:** Any Python package ever published on PyPI can become an MCP server with a single line of code.

---

### 3. `discover()` (Scan an Entire Source Directory)

Scan local codebases with zero source modifications. It automatically detects and exposes:
* Module-level functions, class methods, static methods, and instance methods.
* `@property` descriptors—mapped to MCP *resources*, not tools (e.g., `resource://{module}/{Class}/{property}`).
* Automated class instantiation using config-defined overrides, singleton factories (`get_<Class>()`), or default fallbacks.

```python
app = SmarterMCP("my-codebase-server")
app.discover("./src/mylib", exclude=["test_*"])
app.run()
```

---

### 4. Zero-Code CLI & YAML-driven Manifests

Spin up, validate, and test servers directly from the command line without writing Python wrapper code:

```bash
# Auto-discover a local directory
smarter-mcp serve ./mylib

# Start a decorator-based server script
smarter-mcp serve ./app.py

# Run with a manifest YAML config
smarter-mcp serve --manifest smarter-mcp.yaml
```

Example `smarter-mcp.yaml` manifest:
```yaml
name: chemistry-tools
version: 0.1.0

server:
  host: 0.0.0.0
  port: 8000
  transport: sse

sources:
  - path: ./src/my_local_utils
  - module: random
    include: [choices, randint]
    namespace: random_tools

expose:
  private: false
  inherited: false
  unannotated: warn
  variadic: skip
```

---

## 🛠️ The Production-Grade Runtime Pipeline

Every tool call passes through a rigorous runtime pipeline before executing:

1. **Schema Validation**: Parameters are validated against the JSON schema before functions are called. Clear validation errors are returned to the agent, not raw Python tracebacks.
2. **Type Coercion**: Common agent formatting errors (e.g., sending `"42"` instead of `42`, or a stringified JSON instead of a `dict`) are resolved automatically.
3. **Instance Resolution**: Resolves and binds classes depending on `session`, `singleton`, or `per-call` configurations.
4. **Context Injection**: Automatically injects a FastMCP `Context` parameter if the function accepts it.
5. **Multimodal Interception**: Automatically decodes incoming `ImageContent` parameters into Pillow `PIL.Image.Image` or NumPy `np.ndarray` structures (handling base64 data, URLs, or local files). Returns images back to the agent as wrapped `ImageContent`.
6. **Structured Error Handling**: Catches exceptions and returns them as structured MCP error objects to prevent agent confusion.

---

## 🔄 Gap Comparison: FastMCP vs. Smarter-MCP

| Capability | FastMCP | Smarter-MCP |
| :--- | :--- | :--- |
| **Decorator API** | `@mcp.tool()` (manual wiring) | `@tool` with auto coercion, schema, multimodal |
| **Class Toolkits** | Manual `add_tool()` per method | `@toolkit` with session-scoped instances |
| **Expose Existing Modules** | Not supported | `app.discover_module(random)` |
| **Auto-Discover Codebase** | Not supported | `app.discover("./mylib")` |
| **CLI / YAML config** | Not supported | `smarter-mcp serve`, full manifest |
| **Instance Lifecycle** | Manual | session / singleton / per-call, auto-managed |
| **Type Coercion** | None | `"42"` → `42` automatically |
| **Namespace Routing** | Manual `mount()` | Auto-derived from module paths |
| **Multimodal** | Manual encoding | `PIL.Image`, `ndarray`, `bytes` auto-detected |
| **Tool Name Collisions** | Silent overwrite | Auto-namespaced (`ClassName_method`) |
| **Property → Resource** | Not supported | Automatic mapping |
| **Docstring Parsing** | Raw docstring only | Google/NumPy/Sphinx parsed per-parameter |
| **Type Inference** | None | AST-based inference from defaults + returns |
| **Error Handling** | Raw Python tracebacks | Structured MCP error objects (coercion vs. execution) |
| **HTTP Introspection** | Not supported | `GET /health` + `GET /mcp/{ns}/schema` (with `?compact=true`) |
| **Authentication** | Manual | `X-API-Key` middleware + FastMCP Bearer, config-driven |
| **Rate Limiting** | Manual | Sliding window, per-session + global |
| **LLM Descriptions** | Not supported | Auto-generate missing docs via OpenAI SDK (OpenAI/OpenRouter/Anthropic), cached |
| **Package Export** | Not supported | `smarter-mcp export` → standalone package |
| **`*args/**kwargs`** | Silent runtime failure | Detected, skipped with warning |
| **Schema Validation** | None | Every call validated against JSON schema |

---

## 🧪 How to Setup and Test the Codebase Locally

Follow these steps to run, verify, and test Smarter-MCP locally in a clean Python virtual environment.

### 1. Initialize the Virtual Environment

Run these commands in the project root:
```bash
# Create a virtual environment
python3 -m venv .venv

# Activate the virtual environment
source .venv/bin/activate

# Ensure pip is up to date
pip install --upgrade pip
```

### 2. Install Smarter-MCP with All Dependencies

Install Smarter-MCP in **editable mode** with development and multimodal extras:
```bash
pip install -e ".[all]"
```

### 3. Run the Test Suite

We use `pytest` for unit testing. Execute all tests (CLI command runner, multimodal parser, AST extraction, and testing framework):
```bash
pytest
```

To run individual test files:
```bash
# Verify CLI command configurations
pytest tests/test_cli.py

# Verify multimodal image/blob interception
pytest tests/test_multimodal.py

# Verify tool validation & testing runner
pytest tests/test_testing_framework.py
```

### 4. Verify the CLI Manually

You can test the command line options inside your activated virtual environment:

```bash
# Check CLI help outputs
smarter-mcp --help

# Create a sample python file with tools
cat << 'EOF' > test_server.py
from smarter_mcp import SmarterMCP, tool
from PIL import Image

app = SmarterMCP("test-server")

@tool(tests=[{"params": {"name": "Bob"}, "expect": "Hello, Bob!"}])
def greet(name: str) -> str:
    return f"Hello, {name}!"

@tool()
def process_img(img: Image.Image) -> Image.Image:
    return img.rotate(90)
EOF

# Dry-run validation of the server
smarter-mcp validate test_server.py

# Run the predefined tests inside the file
smarter-mcp test test_server.py

# Serve the server locally (press Ctrl+C to stop)
smarter-mcp serve test_server.py --port 8000
```
Clean up the test server script when done:
```bash
rm test_server.py
```
