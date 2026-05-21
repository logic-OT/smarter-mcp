# Faster-MCP: Remaining Tasks Checklist

This document tracks the features remaining to be built from the `pymcp-spec.md` specification, along with the required tests for each feature.

## 1. Tool Testing Framework
The integrated testing framework ensures that tools are alive, schemas are valid, and return types match expectations.

- [ ] Define `TestResult` dataclass.
- [ ] Implement `FasterMCP.test(tool_name: str | None = None, params: dict | None = None, verbose: bool = False)` method.
- [ ] Add schema validation checks within the test runner.
- [ ] Add instance resolution checks (ensuring class-based tools can instantiate).
- [ ] Add return type and return value verification logic.
- [ ] Implement `faster-mcp test` CLI command.

**Tests to Write:**
- [ ] `test_testing_framework.py`: Verify that passing test cases yield successful `TestResult`s.
- [ ] Verify that failing test cases (type mismatch, exception raised) return failed `TestResult`s.
- [ ] Verify testing a single tool vs testing all tools.

## 2. Multimodal Input/Output Interception
Automatically intercepting MCP `ImageContent` and `BlobContent` and decoding them into Python types like `PIL.Image.Image` and `numpy.ndarray`.

- [ ] Update `coercion.py` (or a dedicated `multimodal.py`) to handle `PIL.Image.Image`, `np.ndarray`, and `bytes`.
- [ ] Update `tool_wrapper.py` to intercept function return values and convert them back to `ImageContent` or `BlobContent`.
- [ ] Add optional `[multimodal]` dependency handling (lazy importing `Pillow` and `numpy`).

**Tests to Write:**
- [ ] `test_multimodal.py`: Verify that sending base64 `ImageContent` successfully coerces into a `np.ndarray` and `PIL.Image`.
- [ ] Verify that returning a `PIL.Image` automatically wraps the response in MCP `ImageContent`.
- [ ] Verify that missing optional dependencies (`Pillow`) raise a clear, helpful error message.

## 3. Command Line Interface (CLI)
The zero-code operational interface for serving, validating, and initializing Faster-MCP servers.

- [ ] Implement `faster-mcp serve <path>` (supports directories, manifest YAMLs, and `.py` files).
- [ ] Implement auto-detection of `FasterMCP` instances in target `.py` files.
- [ ] Implement `faster-mcp validate` (dry run, shows what would be exposed).
- [ ] Implement `faster-mcp init <path>` (scaffolds a `faster-mcp.yaml` manifest).
- [ ] Implement `faster-mcp export` (exports the server as a standalone pip package).
- [ ] Add hot-reloading support (`--dev` flag via `watchfiles`).

**Tests to Write:**
- [ ] `test_cli.py`: Use `click.testing.CliRunner` to verify `serve`, `validate`, and `init` commands.
- [ ] Verify `serve` correctly imports a `.py` file and extracts the `app` instance.
- [ ] Verify `export` generates a valid directory structure and `pyproject.toml`.

## 4. Structured Error Handling
Replacing raw Python tracebacks with structured MCP error objects to improve agent reasoning.

- [ ] Update `tool_wrapper.py` to catch all exceptions and format them into structured JSON/dict error responses.
- [ ] Distinguish between coercion errors (bad input) and execution errors (internal failure).

**Tests to Write:**
- [ ] `test_error_handling.py`: Verify that calling a tool with invalid arguments returns a structured error payload.
- [ ] Verify that raising an internal exception within a tool returns a clean error object instead of crashing the server.

## 5. HTTP Endpoints & Introspection (Optional/Enhancement)
Adding standard REST endpoints to the FastMCP server for operational monitoring.

- [ ] Implement `GET /health` to return uptime, namespaces, and tool counts.
- [ ] Implement `GET /mcp/{namespace}/schema` to return OpenAPI-compatible JSON schemas.

**Tests to Write:**
- [ ] `test_http_endpoints.py`: Use an ASGI test client to ping `/health` and verify the JSON response.
- [ ] Verify `/mcp/default/schema` returns the expected JSON schema structure.

## 6. LLM Description Generation (Optional)
Using an LLM to generate high-quality tool descriptions from unannotated code.

- [ ] Implement `LLMGenerator` using OpenRouter/Gemini.
- [ ] Wire up description generation during extraction if `llm.enabled` is true.
- [ ] Implement caching mechanism to avoid redundant LLM calls.

**Tests to Write:**
- [ ] `test_llm_descriptions.py`: Mock the LLM API call and verify descriptions are injected into the registry.
- [ ] Verify caching behavior.

## 7. Server Auth and Rate Limiting
Implementing production-grade networking features.

- [ ] Wire up API Key authentication using `ServerConfig.auth`.
- [ ] Implement rate limiting middleware (per-session and global).

**Tests to Write:**
- [ ] `test_auth.py`: Verify requests without API keys are rejected.
- [ ] Verify rate limits properly block requests over the threshold.
