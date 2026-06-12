# Smarter-MCP

> **The highest-level Python framework for building, generating, and running MCP servers.**

*If Python can call it, Smarter-MCP can serve it.*

```bash
pip install smarter-mcp
```

---
Smarter-MCP sits on top of FastMCP and acts as the orchestration layer that handles everything a developer shouldn't have to think about:

## Three ways in, one runtime

### 1. Existing code — zero rewrites

Point at any module you already have (or anything on PyPI):

```python
import pandas as pd
from smarter_mcp import SmarterMCP

app = SmarterMCP("data-tools")
app.discover_module(pd.DataFrame, include=["describe", "head", "tail"])
app.run()
```

Or scan an entire local codebase from the CLI:

```bash
smarter-mcp serve ./src/mylib
```

The dual-pass engine (AST + inspect) reads your signatures, builds JSON schemas, and serves them. Nothing to rewrite.

---

### 2. Stateful class tools

When your tools need shared state (a DB connection, an API client, an ML model loaded once), `@toolkit` manages it:

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

One instance per session. Constructor args injected from config. Session instances are evicted via bounded LRU (max 256 entries) with best-effort resource cleanup (`close()`/`__exit__`) on eviction. You write the class, Smarter-MCP handles the plumbing.

---

### 3. ✍️ Fresh tools from scratch

```python
from smarter_mcp import SmarterMCP, tool, resource

app = SmarterMCP("my-server")

@tool("Greet a user by name")
def greet(name: str) -> str:
    return f"Hello, {name}!"

@resource("config://settings")
def get_settings() -> dict:
    return {"debug": True, "version": "1.0"}

app.run()
```

---

## ⚙️ What the runtime gives every tool

Regardless of how a tool was registered, every call goes through:

- ✅ **Schema validation** — parameters validated before your function is called. Clean errors back to the agent, not raw tracebacks.
- ✅ **Type coercion** — agents send `"42"` instead of `42` constantly. Handled silently.
- ✅ **Multimodal** — `PIL.Image` and `np.ndarray` parameters decoded from `ImageContent` automatically. Return images and they're wrapped back up.
- ✅ **Instance lifecycle** — `session`, `singleton`, or `per-call`. Resolved and bound per request.
- ✅ **Namespace routing** — auto-derived from module paths. No silent name collisions.
- ✅ **Auth + rate limiting** — API key middleware and sliding-window rate limits, config-driven.

---

## ✨ AI-generated tool descriptions

Most Python code in the wild has no docstrings. That's fine.

Point Smarter-MCP at an undocumented library and it will write the tool descriptions for you using Claude, GPT, or any OpenAI-compatible model. Every tool your agent sees gets a clean, accurate description regardless of what the original code looked like.

```yaml
llm:
  enabled: true
  provider: anthropic        # or openai, openrouter
  model: claude-3-5-haiku
  cache_path: .smarter-mcp/description-cache.json
```
or in code:

```python
server = SmarterMCP(
    "my-server",
    llm_enabled=True,
    llm_provider="anthropic",
    llm_model="claude-3-5-haiku",
)
server.run()
```

**Undocumented code stops being a blocker.**

---

## 📋 YAML manifest — no Python required

```yaml
name: my-server
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
  include_private: false
  unannotated_policy: warn
```

```bash
smarter-mcp serve --manifest smarter-mcp.yaml
```

---

## 🚀 Getting started

```bash
pip install smarter-mcp
```

For multimodal support (PIL / numpy):

```bash
pip install "smarter-mcp[multimodal]"
```

Then point it at your code:

```bash
smarter-mcp serve ./src/mylib --port 8000
smarter-mcp validate ./my_tools.py
smarter-mcp test ./my_tools.py
```

---

*Built on [FastMCP](https://github.com/jlowin/fastmcp).*
