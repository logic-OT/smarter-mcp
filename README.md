# faster-mcp

Turn any Python codebase into a production-grade MCP server with zero friction.

## Quick Start

```bash
pip install faster-mcp
faster-mcp init --path ./mylib
faster-mcp serve
```

## Features

- **Zero modification**: Your code stays untouched — no decorators required
- **Dual-pass extraction**: AST (safe, no imports) + inspect (runtime accuracy)
- **Namespace routing**: Per-module MCP endpoints via FastMCP mount()
- **Session-scoped instances**: One class instance per MCP connection
- **Multi-format docstrings**: Google, NumPy, and Sphinx parsed automatically
- **Type inference**: Infers types from defaults for unannotated code
- **Multimodal**: PIL.Image, numpy arrays → MCP ImageContent automatically
- **Package export**: Generate standalone distributable MCP server packages

## Requirements

- Python 3.10+
- FastMCP 3.0+
