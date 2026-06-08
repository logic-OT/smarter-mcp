"""
Main server application — wires extraction, filtering, routing, and instances.

This is the primary entry point for programmatic usage:

    from smarter_mcp.server.app import SmarterMCP

    server = SmarterMCP(source_root="./mylib")
    server.run()  # starts SSE server on :8000
"""

from __future__ import annotations

import importlib
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from fastmcp import FastMCP

from smarter_mcp.config.manifest import (
    ManifestConfig,
    default_manifest,
    find_manifest,
    load_manifest,
)
from smarter_mcp.extractor.filters import (
    ExposureRules,
    UnannotatedPolicy,
    VariadicPolicy,
    apply_filters,
)
from smarter_mcp.extractor.models import ExtractionResult, ExtractedModule, ExtractedCallable, CallableKind
from smarter_mcp.extractor.surface import SurfaceExtractor, _SYS_PATH_LOCK
from smarter_mcp.runtime.instances import InstanceManager
from smarter_mcp.server.router import NamespaceRouter
from smarter_mcp._registry import ToolRegistry
from smarter_mcp._testing import ToolTestRunner, TestReport

logger = logging.getLogger(__name__)


def _exposure_rules_from_config(config: ManifestConfig) -> ExposureRules:
    """Convert a loaded manifest exposure configuration into surface-level ExposureRules.

    This helper maps the high-level yaml/manifest exposure configuration fields
    (e.g., whether to include private functions, inherited methods, properties, or respect
    explicit lists) to the granular rules object utilized by the extraction filter engine.

    Args:
        config: The manifest configuration containing tool exposure settings.

    Returns:
        An ExposureRules object containing the parsed and structured exposure logic.
    """
    return ExposureRules(
        include_private=config.expose.include_private,
        include_dunder=config.expose.include_dunder,
        include_inherited=config.expose.include_inherited,
        include_properties=config.expose.include_properties,
        variadic_policy=VariadicPolicy(config.expose.variadic_policy),
        unannotated_policy=UnannotatedPolicy(config.expose.unannotated_policy),
        respect_all=config.expose.respect_all,
        explicit_includes={
            t.function for t in config.tools if t.expose
        },
        explicit_excludes={
            t.function for t in config.tools if not t.expose
        },
    )


def _resolve_implementations(
    result: ExtractionResult,
    source_root: str,
) -> dict[str, Callable]:
    """Import modules and resolve the actual callable python objects from an extraction result.

    This handles dynamic loading of the python modules detected in the source root directory.
    It resolves:
    1. Standalone functions: direct reference to the callable object.
    2. Class methods / instance methods: references to the unbound methods of the class.
       (Binding and lifecycle instantiations are performed at runtime via InstanceManager).
    3. Property getters: references to the underlying getter functions (`fget`).

    This function safely modifies and restores `sys.path` using `_SYS_PATH_LOCK` to allow importing
    modules that reside within the specified source root without permanently polluting `sys.path`.

    Args:
        result: The AST-extracted metadata representing modules, classes, and callables.
        source_root: The file path root directory containing the source code.

    Returns:
        A dictionary mapping qualified callable names (e.g., "pkg.mod.func" or "pkg.mod.Cls.method")
        to their actual Python callable objects.
    """
    impls: dict[str, Callable] = {}

    # Temporarily prepend source_root to sys.path so the modules import, then
    # restore it. sys.path is global, so we snapshot/restore under the shared
    # lock to avoid leaking entries (and to stay safe under parallel builds).
    with _SYS_PATH_LOCK:
        original_path = sys.path.copy()
        try:
            if source_root not in sys.path:
                sys.path.insert(0, source_root)

            for module in result.modules:
                try:
                    runtime_module = importlib.import_module(module.module_name)
                except Exception as e:
                    logger.warning("Cannot import %s: %s", module.module_name, e)
                    continue

                # Resolve functions
                for func in module.functions:
                    runtime_func = getattr(runtime_module, func.simple_name, None)
                    if runtime_func and callable(runtime_func):
                        impls[func.qualified_name] = runtime_func

                # Resolve class methods
                for cls in module.classes:
                    runtime_class = getattr(runtime_module, cls.name, None)
                    if runtime_class is None:
                        continue

                    for method in cls.methods:
                        runtime_method = getattr(runtime_class, method.simple_name, None)
                        if runtime_method:
                            impls[method.qualified_name] = runtime_method

                    # Resolve properties
                    for prop in cls.properties:
                        class_prop = getattr(runtime_class, prop.simple_name, None)
                        if isinstance(class_prop, property):
                            impls[prop.qualified_name] = class_prop.fget
        finally:
            sys.path = original_path

    return impls


class SmarterMCP:
    """Turn any Python codebase into a Model Context Protocol (MCP) server.

    This is the primary user-facing orchestrator. It unifies filesystem exploration,
    static and runtime metadata extraction, exposure filtering, namespace routing,
    instance lifecycle management, rate limiting, token authentication, and optional LLM-assisted
    documentation enrichment.

    Key responsibilities:
    1. **Extraction**: Discovers tools, resources, and toolkits via AST analysis and `inspect`.
    2. **Filtering**: Applies manifest/exposure policies (e.g., unannotated parameters, visibility).
    3. **Routing**: Isolates distinct domains into standard or namespace-prefixed sub-servers.
    4. **Instance Management**: Manages state, dependencies, and lifetimes for object-oriented toolkits.
    5. **Security & Performance**: Applies API key middleware authentication and rate limiting.

    Examples:
        Default file-based auto-discovery:
            >>> server = SmarterMCP(source_root="./src/my_app")
            >>> server.run()  # Starts SSE server on port 8000

        Explicit manifest-driven server:
            >>> server = SmarterMCP(manifest="smarter-mcp.yaml")
            >>> server.run()

        Programmatic configuration with custom settings:
            >>> server = SmarterMCP(
            ...     source_root="./src/my_app",
            ...     name="Customer Support API",
            ...     port=3000,
            ...     transport="sse",
            ...     auth_enabled=True,
            ...     rate_limit_enabled=True,
            ... )
            >>> server.run()
    """

    def __init__(
        self,
        source_root: str | Path | None = None,
        manifest: str | Path | None = None,
        *,
        name: str | None = None,
        port: int | None = None,
        host: str | None = None,
        transport: str | None = None,
        use_inspect: bool = True,
        # Auth
        auth_enabled: bool | None = None,
        auth_header: str | None = None,
        auth_keys_env: str | None = None,
        # Rate limiting
        rate_limit_enabled: bool | None = None,
        rate_limit_per_minute: int | None = None,
        rate_limit_global_per_minute: int | None = None,
        # LLM description generation
        llm_enabled: bool | None = None,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        llm_api_key_env: str | None = None,
    ):
        """Initialize the SmarterMCP server orchestrator.

        Configures server attributes, initializes metadata registries, and loads configuration
        from a manifest file (if provided) or auto-scans the source path. Explicit parameters passed
        as keyword arguments override settings specified in the manifest file.

        Args:
            source_root: File path to the Python source code directory. If not specified, and
                no manifest file is found, the server defaults to decorator-only registration mode.
            manifest: File path to a YAML configuration manifest file (e.g., `smarter-mcp.yaml`).
            name: Optional name for the server. Overrides configuration in manifest.
            port: Port to listen on. Overrides configuration in manifest.
            host: Hostname interface to bind to. Overrides configuration in manifest.
            transport: Underlying communication transport ("sse", "streamable-http", or "stdio").
            use_inspect: If True, uses dynamic import and runtime introspection in addition to
                static AST extraction for richer type info.
            auth_enabled: Explicit override to enable or disable API key header authentication.
            auth_header: Name of the HTTP header verifying clients (default: `X-API-Key`).
            auth_keys_env: Name of the environment variable carrying allowed keys
                (default: `SMARTER_MCP_API_KEYS`).
            rate_limit_enabled: Explicit override to enable/disable rate limiting.
            rate_limit_per_minute: Number of allowed calls per minute per client session.
            rate_limit_global_per_minute: Number of allowed calls per minute across all client sessions.
            llm_enabled: Explicit override to enable/disable LLM-assisted tool documentation.
            llm_provider: LLM platform provider to call ("openai", "openrouter", or "anthropic").
            llm_model: Specific LLM model to query for metadata enrichment.
            llm_api_key_env: Environment variable holding the LLM provider's API token.
        """
        # Load or create manifest
        if manifest:
            self._config = load_manifest(manifest)
        elif source_root:
            found = find_manifest(source_root)
            if found:
                self._config = load_manifest(found)
            else:
                self._config = default_manifest(str(source_root))
        else:
            found = find_manifest(".")
            if found:
                self._config = load_manifest(found)
            else:
                # No source_root and no manifest → decorator-only mode.
                # We deliberately do NOT auto-scan the current directory, which
                # would pull in unintended files (incl. dependencies). Tools must
                # be registered via the standalone @tool/@toolkit decorators, or pass source_root=...
                logger.info(
                    "No source_root or manifest provided — using decorator-"
                    "registered tools only (no filesystem discovery). Pass "
                    "source_root=<your package> or a manifest to auto-discover "
                    "tools from existing code."
                )
                self._config = ManifestConfig()

        # Apply overrides — server
        if name:
            self._config.name = name
        if port:
            self._config.server.port = port
        if host:
            self._config.server.host = host
        if transport:
            self._config.server.transport = transport

        # Auth overrides
        if auth_enabled is not None:
            self._config.server.auth_enabled = auth_enabled
        if auth_header is not None:
            self._config.server.auth_header = auth_header
        if auth_keys_env is not None:
            self._config.server.auth_keys_env = auth_keys_env

        # Rate limit overrides
        if rate_limit_enabled is not None:
            self._config.server.rate_limit_enabled = rate_limit_enabled
        if rate_limit_per_minute is not None:
            self._config.server.rate_limit_per_minute = rate_limit_per_minute
        if rate_limit_global_per_minute is not None:
            self._config.server.rate_limit_global_per_minute = rate_limit_global_per_minute

        # LLM overrides
        if llm_enabled is not None:
            self._config.llm.enabled = llm_enabled
        if llm_provider is not None:
            self._config.llm.provider = llm_provider
        if llm_model is not None:
            self._config.llm.model = llm_model
        if llm_api_key_env is not None:
            self._config.llm.api_key_env = llm_api_key_env

        self._use_inspect = use_inspect
        self._extraction: ExtractionResult | None = None
        self._server: FastMCP | None = None
        self._router: NamespaceRouter | None = None

        self._registry = ToolRegistry()
        self._instance_manager = InstanceManager(self._config.instances)

        # Track which decorator-registered objects this instance has already
        # consumed, so repeated build() calls don't double-register and the
        # global registry is never cleared (clearing it would break sibling
        # SmarterMCP instances in the same process).
        self._registered_decorator_ids: set[int] = set()



    def discover(
        self,
        source_root: str | Path,
        exclude: list[str] | None = None,
        use_cache: bool = False,
    ) -> SmarterMCP:
        """Scan the filesystem within a source directory to discover and register tools.

        Runs AST extraction followed by runtime inspect passes on the detected modules. Excludes
        common test files and patterns automatically unless customized.

        Args:
            source_root: Root directory containing the python code/packages to inspect.
            exclude: Glob patterns or filenames to skip during AST extraction. Defaults to
                filtering test files like `test_*`, `*_test.py`, and `conftest.py`.
            use_cache: If True, uses the disk cache to skip unchanged source files during AST
                extraction, speeding up startup times.

        Returns:
            The SmarterMCP instance itself, allowing chained calls.
        """
        path = Path(source_root).resolve()
        extractor = SurfaceExtractor(
            source_root=path,
            use_inspect=self._use_inspect,
            exclude_patterns=exclude or ["test_*", "*_test.py", "conftest.py"],
            use_cache=use_cache,
        )
        extraction = extractor.extract()
        
        impls = _resolve_implementations(extraction, str(path))
        
        rules = _exposure_rules_from_config(self._config)
        filtered = apply_filters(extraction, rules)
        
        self._registry.merge_extraction(filtered, impls)
        return self

    def discover_module(
        self,
        module: Any,
        *,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        namespace: str | None = None,
    ) -> SmarterMCP:
        """Discover and register tools from an already-imported python module.

        Automatically detects whether the module resides on disk (executing AST extraction
        and filtering) or is a C-extension / standard library module (falling back to inspect-only).

        Args:
            module: The python module object to inspect (e.g., standard libraries or custom packages).
            include: Optional whitelist filter. Only functions matching names in this list are added.
            exclude: Optional blacklist filter. Functions matching names in this list are ignored.
            namespace: Namespace prefix under which the discovered tools will be routed. Defaults
                to the module's name.

        Returns:
            The SmarterMCP instance itself, allowing chained calls.
        """
        source_file = getattr(module, '__file__', None)
        
        if source_file and source_file.endswith('.py'):
            # Full AST extraction
            extractor = SurfaceExtractor(
                source_root=Path(source_file).parent,
                use_inspect=self._use_inspect,
            )
            extracted_mod = extractor.extract_file(Path(source_file))
            extracted = ExtractionResult(modules=[extracted_mod], source_root=str(Path(source_file).parent))
            
            impls = _resolve_implementations(extracted, str(Path(source_file).parent))
            
            # Simple include/exclude filtering — use replace() so the cached
            # ExtractedModule object is never mutated.
            if include:
                mod = extracted.modules[0]
                extracted.modules[0] = replace(
                    mod,
                    functions=[f for f in mod.functions if f.simple_name in include],
                    classes=[
                        replace(c, methods=[m for m in c.methods if m.simple_name in include])
                        for c in mod.classes
                    ],
                )
            if exclude:
                mod = extracted.modules[0]
                extracted.modules[0] = replace(
                    mod,
                    functions=[f for f in mod.functions if f.simple_name not in exclude],
                    classes=[
                        replace(c, methods=[m for m in c.methods if m.simple_name not in exclude])
                        for c in mod.classes
                    ],
                )

            rules = _exposure_rules_from_config(self._config)
            filtered = apply_filters(extracted, rules)
            
            self._registry.merge_extraction(filtered, impls, namespace_override=namespace or module.__name__.split('.')[-1])
        else:
            # Fallback to inspect-only for C-extensions/stdlib
            # For now we create a dummy extraction result
            extracted_mod = ExtractedModule(module_path="", module_name=module.__name__)
            impls = {}
            import inspect as py_inspect
            for name, obj in py_inspect.getmembers(module, py_inspect.isroutine):
                if include and name not in include:
                    continue
                if exclude and name in exclude:
                    continue
                if name.startswith('_'):
                    continue
                
                meta = ExtractedCallable(
                    qualified_name=f"{module.__name__}.{name}",
                    kind=CallableKind.FUNCTION,
                    module_path=""
                )
                extracted_mod.functions.append(meta)
                impls[meta.qualified_name] = obj
                
            extracted = ExtractionResult(modules=[extracted_mod], source_root="")
            self._registry.merge_extraction(extracted, impls, namespace_override=namespace or module.__name__.split('.')[-1])
            
        return self

    def build(self) -> FastMCP:
        """Compile the configuration, extract tools/resources, and build the FastMCP server.

        Processes all listed sources, merges test overrides, executes LLM enrichment if enabled,
        assembles routes and namespaces, attaches middleware (auth, rate limiting), and configures
        custom HTTP endpoints (like `/health` and `/schema`).

        Returns:
            The fully configured and constructed FastMCP server instance.
        """
        # Step 0.5: Register decorator-registered toolkits, tools, and resources.
        # Read from the live global so tools decorated after __init__ are picked
        # up. Use _registered_decorator_ids to skip anything already consumed on
        # a prior build() call, and never clear the global (other SmarterMCP
        # instances in the same process may still need it).
        from smarter_mcp._decorators import (
            get_global_tools,
            get_global_resources,
            get_global_toolkits,
        )

        # 1. Register global toolkits
        for cls in get_global_toolkits():
            if id(cls) in self._registered_decorator_ids:
                continue
            self._registered_decorator_ids.add(id(cls))
            lifecycle = getattr(cls, "_smarter_mcp_lifecycle", "session")
            namespace = getattr(cls, "_smarter_mcp_namespace", "default")
            constructor_args = getattr(cls, "_smarter_mcp_constructor_args", {})
            self._registry.register_toolkit(
                cls, lifecycle=lifecycle, namespace=namespace, constructor_args=constructor_args # type: ignore
            )
            self._instance_manager.add_config(
                class_name=cls.__name__,
                lifecycle=lifecycle,
                args=constructor_args
            )
            for name, fn in cls.__dict__.items():
                if getattr(fn, "_smarter_mcp_tool", False):
                    self._registry.register_tool(
                        fn,
                        name=getattr(fn, "_smarter_mcp_name", None),
                        description=getattr(fn, "_smarter_mcp_description", None),
                        tests=getattr(fn, "_smarter_mcp_tests", []),
                        namespace=namespace,
                        class_name=cls.__name__,
                        source="decorator"
                    )

        # 2. Register global tools (skip toolkit methods already registered above)
        for fn in get_global_tools():
            if id(fn) in self._registered_decorator_ids:
                continue
            is_toolkit_method = False
            for tk in self._registry._toolkits.values():
                if fn in tk.cls.__dict__.values():
                    is_toolkit_method = True
                    break
            if not is_toolkit_method:
                self._registered_decorator_ids.add(id(fn))
                self._registry.register_tool(
                    fn,
                    name=getattr(fn, "_smarter_mcp_name", None),
                    description=getattr(fn, "_smarter_mcp_description", None),
                    tests=getattr(fn, "_smarter_mcp_tests", []),
                    source="decorator"
                )

        # 3. Register global resources (skip toolkit methods already registered above)
        for fn in get_global_resources():
            if id(fn) in self._registered_decorator_ids:
                continue
            is_toolkit_method = False
            for tk in self._registry._toolkits.values():
                if fn in tk.cls.__dict__.values():
                    is_toolkit_method = True
                    break
            if not is_toolkit_method:
                self._registered_decorator_ids.add(id(fn))
                self._registry.register_resource(
                    fn,
                    uri=getattr(fn, "_smarter_mcp_uri", None) or f"resource://default/{fn.__name__}",
                    description=getattr(fn, "_smarter_mcp_description", None),
                    source="decorator"
                )

        # Step 1: Process manifest sources if any
        for source in self._config.sources:
            if source.module:
                import importlib
                try:
                    mod = importlib.import_module(source.module)
                    self.discover_module(mod, include=source.include, exclude=source.exclude, namespace=source.namespace)
                except Exception as e:
                    logger.warning("Could not import module %s: %s", source.module, e)
            elif source.path:
                self.discover(source.path, exclude=source.exclude)

        # Step 2: Wire manifest test cases into the registry.
        # ToolOverride.tests defined in YAML need to be merged into the
        # corresponding RegisteredTool.tests so the test runner can find them.
        # O(1) per override via a single name→tool index (built once).
        if self._config.tools:
            tool_index = self._registry.index_by_name()
            for override in self._config.tools:
                tool = tool_index.get(override.function)
                if tool is not None and override.tests:
                    tool.tests.extend(override.tests)

        # Step 2.5: LLM-assisted description generation (optional).
        # Fills in missing tool descriptions before the server is built so the
        # enriched text flows through to FastMCP registration. Failures here
        # (missing key/package) are non-fatal — the server still builds.
        if self._config.llm.enabled:
            from smarter_mcp.llm.client import LLMNotAvailableError
            from smarter_mcp.llm.generator import LLMGenerator

            try:
                LLMGenerator(self._config.llm).enrich_registry(self._registry)
            except LLMNotAvailableError as e:
                logger.warning("LLM description generation skipped: %s", e)
            except Exception as e:  # noqa: BLE001 - never block server build
                logger.warning("LLM description generation failed: %s", e)

        # Step 3: Build router + server
        from smarter_mcp.server.security import (
            build_auth_provider,
            build_rate_limit_middleware,
        )

        self._router = NamespaceRouter(
            config=self._config,
            instance_manager=self._instance_manager,
        )
        auth = build_auth_provider(self._config.server)
        self._server = self._router.build_server(self._registry, auth=auth)

        # Attach rate-limiting middleware (MCP-level, per-session + global)
        for mw in build_rate_limit_middleware(self._config.server):
            self._server.add_middleware(mw)

        # Register custom HTTP endpoints (health + schema introspection)
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from smarter_mcp.server.health import HealthEndpoint
        from smarter_mcp.server.schema_endpoint import SchemaEndpoint

        _health_ep = HealthEndpoint(self._router, self._registry)
        _schema_ep = SchemaEndpoint(self._registry)

        @self._server.custom_route("/health", methods=["GET"])
        async def _health_handler(request: Request) -> JSONResponse:
            return JSONResponse(_health_ep.get_health())

        @self._server.custom_route("/mcp/{namespace}/schema", methods=["GET"])
        async def _schema_handler(request: Request) -> JSONResponse:
            ns = request.path_params["namespace"]
            compact = request.query_params.get("compact", "false").lower() == "true"
            return JSONResponse(_schema_ep.get_namespace_schema(ns, compact=compact))

        logger.info(
            "Server '%s' ready: %d namespaces, %s transport",
            self._config.name,
            len(self._router.namespaces),
            self._config.server.transport,
        )

        return self._server

    def test(
        self,
        tool_name: str | None = None,
        *,
        params: dict[str, Any] | None = None,
        verbose: bool = False,
    ) -> TestReport:
        """Run tool tests.

        Three calling patterns:
            app.test()                                  # all predefined tests
            app.test("greet")                            # predefined tests for one tool
            app.test("greet", params={"name": "Alice"})  # ad-hoc test

        Args:
            tool_name: Name of a specific tool to test. None = test all.
            params: Ad-hoc parameters to test with (requires tool_name).
            verbose: If True, log individual test results.

        Returns:
            TestReport with results, pass/fail counts, and skipped count.
        """
        if self._server is None:
            self.build()

        runner = ToolTestRunner(
            registry=self._registry,
            instance_manager=self._instance_manager,
        )

        if tool_name and params:
            report = runner.run_adhoc(tool_name, params)
        elif tool_name:
            report = runner.run_tool(tool_name)
        else:
            report = runner.run_all()

        if verbose:
            self._print_test_report(report)

        return report

    def _print_test_report(self, report: TestReport) -> None:
        """Log a human-readable representation of a test execution report.

        Outputs the status of each executed test, latency measurements, return values,
        and failure tracebacks to the logger.

        Args:
            report: The compiled TestReport to print.
        """
        logger.info("Testing %d tool(s)...", report.total + report.skipped)
        for result in report.results:
            status = "✓" if result.passed else "✗"
            output_repr = repr(result.output)[:60] if result.output is not None else ""
            msg = (
                f"  {status} {result.namespace}/{result.tool_name}  "
                f"({result.latency_ms:.0f}ms)"
            )
            if output_repr:
                msg += f"  → {output_repr}"
            if result.error:
                msg += f"  ERROR: {result.error}"
            logger.info(msg)
        logger.info(report.summary())

    def _asgi_middleware(self) -> list:
        """Construct ASGI middleware to enforce authentication rules.

        Loads allowed API keys from the configured environment variables and builds
        the starlette APIKeyMiddleware wrapper.

        Returns:
            A list containing Starlette Middleware objects if auth is enabled, or an empty list.
        """
        if not self._config.server.auth_enabled:
            return []

        from starlette.middleware import Middleware
        from smarter_mcp.server.security import APIKeyMiddleware, load_api_keys

        keys = load_api_keys(self._config.server.auth_keys_env)
        return [
            Middleware(
                APIKeyMiddleware,
                header_name=self._config.server.auth_header,
                valid_keys=keys,
            )
        ]

    def http_app(self) -> Any:
        """Construct and return the Starlette ASGI application with configured middlewares.

        This compiles the FastMCP server (if not already built) and extracts its starlette-compatible
        routing application, attaching API key validation middleware. Useful for embedding inside
        larger FastAPI/ASGI servers or when running testing clients.

        Returns:
            A Starlette ASGI application instance.
        """
        if self._server is None:
            self.build()
        return self._server.http_app(middleware=self._asgi_middleware())

    def run(self) -> None:
        """Compile, configure, and start the MCP server, blocking execution.

        Ensures the server is built, resolves the transport protocol from the manifest
        ("stdio", "sse", or "streamable-http"), applies middleware, and boots the Uvicorn-based
        FastMCP server.

        Raises:
            ValueError: If an unrecognized transport protocol is configured.
        """
        if self._server is None:
            self.build()

        transport = self._config.server.transport

        if transport == "stdio":
            self._server.run(transport="stdio")
        elif transport == "sse":
            self._server.run(
                transport="sse",
                host=self._config.server.host,
                port=self._config.server.port,
                middleware=self._asgi_middleware(),
            )
        elif transport == "streamable-http":
            self._server.run(
                transport="streamable-http",
                host=self._config.server.host,
                port=self._config.server.port,
                middleware=self._asgi_middleware(),
            )
        else:
            raise ValueError(f"Unknown transport: {transport}")

    @property
    def server(self) -> FastMCP | None:
        """The underlying FastMCP server instance."""
        return self._server

    @property
    def config(self) -> ManifestConfig:
        """The active manifest configuration."""
        return self._config

    @property
    def extraction_result(self) -> ExtractionResult | None:
        """The raw extraction result (available after build())."""
        return self._extraction

