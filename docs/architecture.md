# Smarter-MCP Architecture

This document explains how Smarter-MCP turns Python code into a FastMCP server.
It focuses on the current code in this repository, not only the long-term product
vision.

## Big Picture

Smarter-MCP has three major jobs:

1. Find Python callables that can become MCP tools or resources.
2. Normalize them into one internal registry.
3. Build a FastMCP server that wraps calls with coercion, instance handling, and
   multimodal conversion.

```mermaid
flowchart LR
    UserCode["Python codebase<br/>functions, classes, properties"]
    Decorators["@tool<br/>@resource<br/>@toolkit"]
    Manifest["smarter-mcp.yaml<br/>sources, routing, instances, tests"]
    CLI["smarter-mcp CLI<br/>serve, validate, test, init"]

    Extractor["SurfaceExtractor<br/>AST + inspect"]
    Filters["Exposure filters<br/>private, inherited, variadic, unannotated"]
    Registry["ToolRegistry<br/>single source of truth"]
    Router["NamespaceRouter<br/>FastMCP root + subservers"]
    Runtime["Runtime wrappers<br/>coercion, context, instances, images"]
    FastMCP["FastMCP server<br/>stdio, sse, streamable-http"]

    UserCode --> Extractor
    Decorators --> Registry
    Manifest --> Extractor
    Manifest --> Filters
    Manifest --> Router
    Manifest --> Runtime
    CLI --> Manifest
    CLI --> Extractor

    Extractor --> Filters --> Registry --> Router --> Runtime --> FastMCP
```

## Package Map

```mermaid
flowchart TB
    Root["src/smarter_mcp"]

    Root --> App["server/app.py<br/>SmarterMCP facade"]
    Root --> Config["config/manifest.py<br/>Pydantic YAML models"]
    Root --> Extractor["extractor/*<br/>source discovery and metadata"]
    Root --> Registry["_registry.py<br/>registered tools/resources/toolkits"]
    Root --> Schema["_schema.py<br/>JSON schema builder"]
    Root --> Runtime["runtime/*<br/>wrappers, coercion, instances"]
    Root --> Multi["multimodal/*<br/>image input/output bridge"]
    Root --> Router["server/router.py<br/>namespace FastMCP mounting"]
    Root --> CLI["cli/*<br/>click commands and target detection"]
    Root --> Testing["_testing.py<br/>tool test runner"]
    Root --> Errors["errors.py<br/>structured error types + formatter"]
    Root --> Security["server/security.py<br/>API-key auth + rate limiting"]
    Root --> Endpoints["server/health.py + schema_endpoint.py<br/>HTTP introspection"]
    Root --> Export["export/*<br/>export placeholder"]
    Root --> LLM["llm/*<br/>description generation (OpenAI SDK)"]
```

## Entry Points

There are four ways code enters the system.

```mermaid
flowchart LR
    A["Standalone decorators<br/>@tool, @resource, @toolkit"]
    C["Directory discovery<br/>app.discover('./src')"]
    D["Module discovery<br/>app.discover_module(random)"]
    E["CLI target<br/>smarter-mcp serve/validate/test"]

    A --> Registry["ToolRegistry"]
    C --> Extract["SurfaceExtractor"]
    D --> Extract
    E --> Detect["resolve_target()"]
    Detect --> C
    Detect --> Manifest["Manifest config"]
    Manifest --> C
    Extract --> Registry
```

### Programmatic API

The main user-facing class is `SmarterMCP` in `server/app.py`.

```python
from smarter_mcp import SmarterMCP, tool

app = SmarterMCP("example")

@tool()
def add(a: int, b: int) -> int:
    return a + b

app.run()
```

### CLI API

The CLI is defined in `cli/main.py`.
 start a server
smarter-mcp validate <target>    build and print exposed tools/resources
smarter-mcp test <target>       
```text
smarter-mcp serve <target>       run registered test cases
smarter-mcp init <path>          scaffold smarter-mcp.yaml
smarter-mcp export               placeholder command
```

## Build Lifecycle

`SmarterMCP.build()` is the central assembly line.

```mermaid
sequenceDiagram
    participant User
    participant App as SmarterMCP
    participant Config as ManifestConfig
    participant Extract as SurfaceExtractor
    participant Filters as apply_filters
    participant Registry as ToolRegistry
    participant Router as NamespaceRouter
    participant FastMCP

    User->>App: build() or run()
    App->>Config: load/find/default manifest
    loop each source
        App->>Extract: extract directory/module
        Extract-->>App: ExtractionResult
        App->>Filters: apply exposure rules
        Filters-->>App: filtered ExtractionResult
        App->>Registry: merge_extraction()
    end
    App->>Registry: merge manifest tests
    opt llm.enabled
        App->>Registry: LLMGenerator.enrich_registry() (fill missing descriptions)
    end
    App->>Router: build_server(registry, auth=build_auth_provider(...))
    Router->>FastMCP: create root server (with Bearer auth)
    loop each namespace
        Router->>FastMCP: create subserver
        Router->>FastMCP: register wrapped tools/resources
        Router->>FastMCP: mount subserver
    end
    App->>FastMCP: add rate-limit middleware (per-session + global)
    App->>FastMCP: register /health + /mcp/{ns}/schema routes
    FastMCP-->>User: runnable server
```

`run()` and `http_app()` additionally wrap the ASGI app with the `X-API-Key`
middleware when `auth_enabled` is set. LLM enrichment, auth, and rate limiting
are all opt-in and fail-soft: a missing LLM key/package logs a warning and the
build proceeds.

## Control Plane: Manifest Config

`config/manifest.py` defines the YAML control plane. The manifest decides what
to scan, what to expose, how to route namespaces, and how class instances should
be created.

```mermaid
flowchart TB
    Manifest["ManifestConfig"]
    Manifest --> Server["server<br/>host, port, transport<br/>auth fields, rate-limit fields"]
    Manifest --> Sources["sources<br/>path or module<br/>include/exclude namespace"]
    Manifest --> Routing["routing<br/>base path, overrides, separator"]
    Manifest --> Expose["expose<br/>private, inherited, properties,<br/>variadic, unannotated"]
    Manifest --> Instances["instances<br/>class lifecycle and constructor/factory args"]
    Manifest --> Tools["tools<br/>name, description, expose flag, tests"]
    Manifest --> Multimodal["multimodal<br/>image behavior config"]
    Manifest --> LLM["llm<br/>provider, model, cache, overwrite"]
```

The `server` auth and rate-limit fields are fully wired: `build()` constructs a
Bearer auth provider and rate-limit middleware from them, and `http_app()`/`run()`
attach the `X-API-Key` ASGI middleware. The `llm` block drives optional
description generation during `build()`. See
[Server Security](#server-security-auth--rate-limiting) and
[LLM Description Generation](#llm-description-generation) below.

## Extraction Engine

The extraction engine lives in `extractor/surface.py` and produces the metadata
models from `extractor/models.py`.

It uses two passes:

1. AST pass: parse source files without importing them.
2. Inspect pass: optionally import modules to improve signatures and detect
   runtime details.

```mermaid
flowchart LR
    PyFile["Python file"]
    AST["AST pass<br/>safe static parse"]
    Inspect["inspect pass<br/>runtime import when enabled"]
    Docstrings["docstring parser<br/>Google, NumPy, Sphinx, plain"]
    Inference["type inference<br/>defaults and returns"]
    Models["ExtractionResult<br/>ExtractedModule<br/>ExtractedClass<br/>ExtractedCallable<br/>ExtractedParam"]

    PyFile --> AST
    PyFile --> Inspect
    AST --> Docstrings
    Inspect --> Docstrings
    Docstrings --> Inference
    Inference --> Models
```

The core intermediate representation looks like this:

```mermaid
classDiagram
    class ExtractionResult {
        modules
        warnings
        errors
        total_tools
        total_resources
    }
    class ExtractedModule {
        module_path
        module_name
        functions
        classes
        all_exports
    }
    class ExtractedClass {
        name
        methods
        properties
        init_params
    }
    class ExtractedCallable {
        qualified_name
        kind
        class_name
        is_async
        parameters
        return_type
        docstring
        tool_name
    }
    class ExtractedParam {
        name
        annotation
        default
        kind
        description
        inferred_type
        effective_type
    }

    ExtractionResult "1" --> "*" ExtractedModule
    ExtractedModule "1" --> "*" ExtractedCallable
    ExtractedModule "1" --> "*" ExtractedClass
    ExtractedClass "1" --> "*" ExtractedCallable
    ExtractedCallable "1" --> "*" ExtractedParam
```

## Filtering

After extraction, `extractor/filters.py` applies exposure rules. This is where
the system decides which discovered callables are safe and useful enough to
publish.

```mermaid
flowchart TD
    Candidate["Extracted callable"]
    Private{"private or dunder?"}
    Inherited{"inherited?"}
    Variadic{"has *args/**kwargs?"}
    Annotated{"missing annotations?"}
    Property{"property?"}
    Keep["Keep as tool/resource"]
    Skip["Skip"]
    Warn["Keep but warn"]

    Candidate --> Private
    Private -- disallowed --> Skip
    Private -- allowed --> Inherited
    Inherited -- disallowed --> Skip
    Inherited -- allowed --> Variadic
    Variadic -- skip --> Skip
    Variadic -- warn --> Warn
    Variadic -- expose --> Annotated
    Warn --> Annotated
    Annotated -- skip --> Skip
    Annotated -- warn/expose --> Property
    Property -- include_properties --> Keep
    Property -- disabled --> Skip
```

## Registry: The Internal Source of Truth

`_registry.py` stores everything after decorators and discovery converge.

```mermaid
flowchart TB
    Registry["ToolRegistry"]
    Registry --> Tools["RegisteredTool<br/>name, description, fn, namespace,<br/>source, class_name, tests, extracted_obj"]
    Registry --> Resources["RegisteredResource<br/>uri, description, fn, namespace"]
    Registry --> Toolkits["RegisteredToolkit<br/>class, lifecycle, constructor args, tools"]

    Decorator["Decorator registration"] --> Registry
    Discovery["Discovery registration"] --> Registry
    ManifestTests["Manifest test cases"] --> Tools
```

The registry deliberately separates "what exists" from "how FastMCP serves it".
That makes the same metadata usable by:

- `NamespaceRouter` for serving
- `_schema.py` for schema generation
- `_testing.py` for tool tests
- CLI validation output

## Routing and FastMCP Mounting

`server/router.py` turns registry entries into FastMCP servers. The root server
mounts one subserver per namespace.

```mermaid
flowchart TB
    Root["FastMCP root<br/>name from manifest"]

    Root --> NS1["namespace: default<br/>FastMCP subserver"]
    Root --> NS2["namespace: image_tools<br/>FastMCP subserver"]
    Root --> NS3["namespace: db_client<br/>FastMCP subserver"]

    NS1 --> T1["tool: add"]
    NS1 --> T2["tool: greet"]
    NS2 --> T3["tool: process_image"]
    NS3 --> T4["tool: DatabaseClient_query"]
    NS3 --> R1["resource: resource://db_client/DatabaseClient/status"]
```

Tool names are generated as follows:

| Python surface | MCP name |
|---|---|
| `def add(...)` | `add` |
| `class Client: def query(...)` | `Client_query` |
| Manifest override with `name:` | override wins |

## Runtime Call Path

Every served tool is wrapped by `runtime/tool_wrapper.py` before FastMCP receives
it.

```mermaid
sequenceDiagram
    participant Agent as MCP client/agent
    participant FastMCP
    participant Wrapper as tool_wrapper
    participant Coercion as coerce_arguments
    participant Instances as InstanceManager
    participant Impl as user function/method
    participant Multi as multimodal interceptor

    Agent->>FastMCP: call tool with JSON args
    FastMCP->>Wrapper: invoke wrapper(ctx, **kwargs)
    Wrapper->>Coercion: coerce strings and image inputs
    alt class method
        Wrapper->>Instances: get instance for lifecycle
        Instances-->>Wrapper: instance
        Wrapper->>Impl: impl(instance, **coerced)
    else function
        Wrapper->>Impl: impl(**coerced)
    end
    Impl-->>Wrapper: Python return value
    Wrapper->>Multi: convert images/arrays/bytes if needed
    Multi-->>FastMCP: MCP-friendly return value
    FastMCP-->>Agent: result
```

Error behavior: wrappers catch exceptions and **return** a structured error
object rather than letting a raw traceback escape to the agent. `CoercionError`
becomes a `coercion_error` payload (bad input); any other exception becomes an
`execution_error` payload. See [Structured Error Handling](#structured-error-handling).

## Type Coercion

`runtime/coercion.py` tries to adapt common LLM/client argument shapes to the
Python types expected by the underlying callable.

```mermaid
flowchart LR
    Raw["Raw JSON/FastMCP arg"]
    Type["Expected Python type"]
    Coerce["coerce_arguments()"]
    Out["Python value"]

    Raw --> Coerce
    Type --> Coerce
    Coerce --> Out

    S1["'42' + int"] --> I1["42"]
    S2["'true' + bool"] --> I2["True"]
    S3["'{\"a\":1}' + dict"] --> I3["{'a': 1}"]
    S4["'[1,2]' + list"] --> I4["[1, 2]"]
    S5["base64/path/url + PIL.Image"] --> I5["PIL.Image.Image"]
    S6["base64/path/url + ndarray"] --> I6["numpy.ndarray"]
```

If coercion fails for a simple type, `coercion.py` raises `CoercionError`. The
tool wrapper catches it and returns a structured `coercion_error` response, so
the agent gets a clear "bad input" signal it can act on instead of a traceback.

## Instance Lifecycle

`runtime/instances.py` controls class construction for class-based tools.

```mermaid
flowchart TB
    Call["Class method tool call"]
    Config{"InstanceConfig exists?"}
    Lifecycle{"lifecycle"}

    Call --> Config
    Config -- no --> Default["try cls()"]
    Config -- yes --> Lifecycle

    Lifecycle -- singleton --> Singleton["one global instance<br/>stored in _singletons"]
    Lifecycle -- session --> Session["one per FastMCP request context<br/>fallback to per-call without ctx"]
    Lifecycle -- per-call --> PerCall["new instance each call"]

    Singleton --> Factory{"factory?"}
    Session --> Factory
    PerCall --> Factory
    Default --> Construct["construct instance"]
    Factory -- yes --> FactoryCall["call configured factory"]
    Factory -- no --> Args["call cls(**constructor_args)"]
    FactoryCall --> Construct
    Args --> Construct
```

Lifecycle choices:

| Lifecycle | Good for | Trade-off |
|---|---|---|
| `session` | per-client state, authenticated clients, caches | needs MCP context; direct tests fall back |
| `singleton` | shared clients, expensive setup | shared mutable state must be safe |
| `per-call` | stateless utilities, simple isolation | more construction overhead |

## Multimodal Handling

`multimodal/interceptor.py` bridges Python image objects and FastMCP image
content.

```mermaid
flowchart LR
    Input["Incoming arg<br/>path, URL, base64, data URL, dict"]
    Resolve["resolve_image_input()"]
    PIL["PIL.Image.Image"]
    NP["numpy.ndarray"]

    Output["Tool return<br/>PIL image, ndarray, bytes, path"]
    Convert["coerce_to_fastmcp_image()"]
    MCPImage["fastmcp.Image"]

    Input --> Resolve
    Resolve --> PIL
    Resolve --> NP

    Output --> Convert
    Convert --> MCPImage
```

The multimodal dependencies are optional. Pillow and NumPy are imported lazily,
and missing extras raise a clearer installation message when an image conversion
actually needs them.

## Schema Generation

`_schema.py` builds JSON schemas from either extracted metadata or live function
signatures.

```mermaid
flowchart TD
    Tool["RegisteredTool"]
    HasExtracted{"tool.extracted_obj?"}
    ExtractedSchema["_schema_from_extracted()"]
    SignatureSchema["_schema_from_signature()"]
    JSON["JSON schema<br/>type=object<br/>properties + required"]

    Tool --> HasExtracted
    HasExtracted -- yes --> ExtractedSchema
    HasExtracted -- no --> SignatureSchema
    ExtractedSchema --> JSON
    SignatureSchema --> JSON
```

Multimodal parameters are exposed as strings in schema output, with descriptions
that hint at file paths or remote URLs. The runtime wrapper also rewrites complex
image annotations to `str` in its public signature to keep FastMCP/Pydantic
registration compatible.

## Tool Testing Framework

`_testing.py` lets developers verify registered tools before exposing them to
agents.

```mermaid
flowchart LR
    Tests["Decorator/YAML tests<br/>params, expect, expect_type"]
    Runner["ToolTestRunner"]
    Checks["checks<br/>callable, schema, instance,<br/>execution, return type,<br/>return value, serializable"]
    Result["TestResult"]
    Report["TestReport"]
    CLI["smarter-mcp test"]

    Tests --> Runner
    Runner --> Checks
    Checks --> Result
    Result --> Report
    Report --> CLI
```

The same runner powers:

- `app.test()`
- `app.test("tool_name")`
- `app.test("tool_name", params={...})`
- `smarter-mcp test`

## Structured Error Handling

`errors.py` defines the error hierarchy and the JSON formatter. The runtime
wrappers in `runtime/tool_wrapper.py` use them to turn any failure into a
machine-readable object.

```mermaid
flowchart TD
    Call["Tool call"]
    Coerce["coerce_arguments()"]
    Impl["user function"]
    CErr["CoercionError"]
    XErr["any Exception"]
    Fmt["format_error_response()"]
    Resp["structured payload<br/>error, error_type, tool, message, details"]

    Call --> Coerce
    Coerce -- ok --> Impl
    Coerce -- fail --> CErr
    Impl -- raises --> XErr
    CErr --> Fmt
    XErr --> Fmt
    Fmt --> Resp
```

| Source | `error_type` | Logged at |
|---|---|---|
| `CoercionError` (bad input) | `coercion_error` | `warning` |
| Any other exception (internal) | `execution_error` | `error` (+ traceback) |

The agent only ever sees the clean payload; full tracebacks stay in the logs.

## HTTP Endpoints & Introspection

`server/health.py` and `server/schema_endpoint.py` back two custom Starlette
routes registered on the FastMCP server during `build()`.

```mermaid
flowchart LR
    HealthEP["HealthEndpoint<br/>reads ToolRegistry"]
    SchemaEP["SchemaEndpoint<br/>reads ToolRegistry + _schema"]
    H["GET /health"]
    S["GET /mcp/{namespace}/schema"]

    HealthEP --> H
    SchemaEP --> S
    S -.->|"?compact=true"| Compact["names + params only"]
```

- `/health` reports `status`, `name`, `namespaces`, `tool_count`, `resource_count`
  (counts read straight from the registry). Always exempt from auth.
- `/mcp/{namespace}/schema` returns OpenAPI 3.1 JSON; `?compact=true` returns a
  trimmed `{namespace, tools:[{name, params}]}` for large surfaces. Unknown
  namespaces return `{"error": ...}`.

## Server Security: Auth & Rate Limiting

`server/security.py` centralizes all security construction so `build()`,
`http_app()`, and `run()` share one source of truth. Everything is off unless
enabled in the `server` config.

```mermaid
flowchart TB
    Keys["load_api_keys(env)<br/>comma-separated"]
    AuthP["build_auth_provider()<br/>StaticTokenVerifier (Bearer)"]
    ASGI["APIKeyMiddleware<br/>X-API-Key, exempts /health"]
    Rate["build_rate_limit_middleware()<br/>sliding window x2"]

    Keys --> AuthP
    Keys --> ASGI
    AuthP -->|"root FastMCP(auth=...)"| Root["MCP-protocol clients"]
    ASGI -->|"http_app()/run()"| HTTP["HTTP routes"]
    Rate -->|"server.add_middleware()"| PerSession["per-session limit"]
    Rate --> Global["global limit"]
```

- **Auth (two layers, one key set):** a custom `X-API-Key` ASGI middleware guards
  HTTP routes, and a FastMCP-native `StaticTokenVerifier` guards the MCP protocol
  (`Authorization: Bearer <key>`). Keys come from `auth_keys_env`.
- **Rate limiting:** two `SlidingWindowRateLimitingMiddleware` instances — one
  per-session (keyed by MCP session id), one global. Attaching to the server
  object means limits also apply to in-memory `fastmcp.Client` connections.

## LLM Description Generation

`llm/generator.py` enriches the registry with LLM-written descriptions for tools
that lack them; `llm/client.py` is the OpenAI-SDK backend.

```mermaid
flowchart LR
    Gen["LLMGenerator"]
    Need{"needs description?<br/>(missing, or overwrite_existing)"}
    Cache{"in disk cache?"}
    Client["OpenAIClient<br/>openai/openrouter/anthropic"]
    Write["set tool.description"]
    Save["persist cache JSON"]

    Gen --> Need
    Need -- no --> Skip["skip"]
    Need -- yes --> Cache
    Cache -- hit --> Write
    Cache -- miss --> Client --> Write
    Write --> Save
```

- Runs during `build()` when `llm.enabled`, after discovery and before routing.
- **Cache** keyed by `sha256(signature + docstring)` — unchanged code = zero calls.
- **Fail-soft:** a missing key/`openai` package logs a warning and the build
  proceeds; the client is built lazily, so nothing is required if no tool needs a
  description.
- v1 uses the OpenAI SDK only; `provider` selects the base URL + key env var
  (`openai` / `openrouter` / `anthropic`).

## Current Feature Status

```mermaid
flowchart LR
    Done["Implemented and tested"]
    Partial["Partial / config exists"]
    Planned["Remaining checklist"]

    Done --> Testing["Tool testing framework"]
    Done --> Multi["Multimodal interception"]
    Done --> CLI["CLI serve/validate/init/test/export stub"]
    Done --> Extract["Discovery/extraction/filtering"]
    Done --> Routing["Namespace routing"]
    Done --> Errors["Structured error responses"]
    Done --> Health["HTTP health/schema endpoints"]
    Done --> AuthImpl["Auth (X-API-Key + Bearer)"]
    Done --> RateImpl["Rate limiting (sliding window)"]
    Done --> LLMImpl["LLM description generation (OpenAI SDK)"]

    Partial --> ExportStub["Export command stub"]

    Planned --> ExportImpl["Package export implementation"]
    Planned --> LLMv2["LLM v2 (LiteLLM, param-level descriptions)"]
```

## End-to-End Mental Model

Think of Smarter-MCP as a compiler-like pipeline:

```text
Python source/decorators/YAML
        |
        v
Extraction + registration
        |
        v
Internal registry
        |
        v
FastMCP routing
        |
        v
Runtime wrapper
        |
        v
User code executes
        |
        v
MCP-friendly response
```

The most important boundary is the registry. Everything before it is about
discovering and normalizing Python surfaces. Everything after it is about
serving those surfaces safely and ergonomically through FastMCP.

