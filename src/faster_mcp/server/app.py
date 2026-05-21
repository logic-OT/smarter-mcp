"""
Main server application — wires extraction, filtering, routing, and instances.

This is the primary entry point for programmatic usage:

    from faster_mcp.server.app import FasterMCP

    server = FasterMCP(source_root="./mylib")
    server.run()  # starts SSE server on :8000
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from typing import Any, Callable

from fastmcp import FastMCP

from faster_mcp.config.manifest import (
    ManifestConfig,
    default_manifest,
    find_manifest,
    load_manifest,
)
from faster_mcp.extractor.filters import (
    ExposureRules,
    UnannotatedPolicy,
    VariadicPolicy,
    apply_filters,
)
from faster_mcp.extractor.models import ExtractionResult, ExtractedModule, ExtractedCallable, CallableKind
from faster_mcp.extractor.surface import SurfaceExtractor
from faster_mcp.runtime.instances import InstanceManager
from faster_mcp.server.router import NamespaceRouter
from faster_mcp._registry import ToolRegistry
from faster_mcp._testing import ToolTestRunner, TestReport

logger = logging.getLogger(__name__)


def _exposure_rules_from_config(config: ManifestConfig) -> ExposureRules:
    """Convert manifest expose config to ExposureRules."""
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
    """Import modules and resolve actual callable implementations.

    For functions: directly get the function object.
    For methods: get the unbound method from the class (binding happens
    at runtime via the instance manager).
    """
    impls: dict[str, Callable] = {}

    # Ensure source root is in path
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

    return impls


class FasterMCP:
    """Turn any Python codebase into an MCP server.

    This is the main user-facing class. It orchestrates:
    1. Extraction (AST + inspect)
    2. Filtering (exposure rules)
    3. Routing (namespace sub-servers)
    4. Instance management (for class methods)

    Usage:
        server = FasterMCP(source_root="./mylib")
        server.run()  # SSE on :8000

        # Or with a manifest:
        server = FasterMCP(manifest="faster-mcp.yaml")
        server.run()

        # Or programmatic configuration:
        server = FasterMCP(
            source_root="./mylib",
            name="My Tools",
            port=3000,
            transport="sse",
        )
        server.run()
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
    ):
        """
        Args:
            source_root: Path to the Python source directory.
            manifest: Path to a faster-mcp.yaml manifest file.
            name: Server name (overrides manifest).
            port: Server port (overrides manifest).
            host: Server host (overrides manifest).
            transport: Transport type: "sse", "streamable-http", or "stdio".
            use_inspect: Whether to use the inspect pass (requires importing).
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
                self._config = default_manifest(".")

        # Apply overrides
        if name:
            self._config.name = name
        if port:
            self._config.server.port = port
        if host:
            self._config.server.host = host
        if transport:
            self._config.server.transport = transport

        self._use_inspect = use_inspect
        self._extraction: ExtractionResult | None = None
        self._server: FastMCP | None = None
        self._router: NamespaceRouter | None = None
        
        self._registry = ToolRegistry()
        self._instance_manager = InstanceManager(self._config.instances)

    def tool(self, name: str | None = None, description: str | None = None, tests: list[dict] | None = None) -> Callable:
        """Register a function or method as an MCP tool."""
        def decorator(fn: Callable) -> Callable:
            self._registry.register_tool(
                fn, name=name, description=description, tests=tests, source="decorator"
            )
            return fn
        return decorator

    def resource(self, uri: str, description: str | None = None) -> Callable:
        """Register a function or method as an MCP resource."""
        def decorator(fn: Callable) -> Callable:
            self._registry.register_resource(
                fn, uri=uri, description=description, source="decorator"
            )
            return fn
        return decorator

    def toolkit(self, lifecycle: str = "session", namespace: str = "default", constructor_args: dict | None = None) -> Callable:
        """Register a class as an MCP toolkit."""
        def decorator(cls: type) -> type:
            self._registry.register_toolkit(
                cls, lifecycle=lifecycle, namespace=namespace, constructor_args=constructor_args # type: ignore
            )
            self._instance_manager.add_config(
                class_name=cls.__name__,
                lifecycle=lifecycle,
                args=constructor_args
            )
            
            # Find any @app.tool methods already registered from this class
            for ns, tools in self._registry._tools.items():
                for tool in tools.values():
                    # Unbound functions match the class __dict__
                    if tool.fn in cls.__dict__.values():
                        tool.class_name = cls.__name__
                        tool.namespace = namespace

            # Find standalone @tool methods in the class that weren't registered yet
            for name, fn in cls.__dict__.items():
                if getattr(fn, "_faster_mcp_tool", False):
                    # Check if already registered by @app.tool
                    already_registered = any(
                        t.fn == fn 
                        for ns_tools in self._registry._tools.values() 
                        for t in ns_tools.values()
                    )
                    if not already_registered:
                        self._registry.register_tool(
                            fn,
                            name=getattr(fn, "_faster_mcp_name", None),
                            description=getattr(fn, "_faster_mcp_description", None),
                            tests=getattr(fn, "_faster_mcp_tests", []),
                            namespace=namespace,
                            class_name=cls.__name__,
                            source="decorator"
                        )

            return cls
        return decorator

    def discover(self, source_root: str | Path, exclude: list[str] | None = None) -> FasterMCP:
        """Scan a codebase and register discovered tools."""
        path = Path(source_root).resolve()
        extractor = SurfaceExtractor(
            source_root=path,
            use_inspect=self._use_inspect,
            exclude_patterns=exclude or ["test_*", "*_test.py", "conftest.py"],
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
    ) -> FasterMCP:
        """Register tools from an already-imported Python module."""
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
            
            # Simple include/exclude filtering
            if include:
                extracted.modules[0].functions = [f for f in extracted.modules[0].functions if f.simple_name in include]
                for cls in extracted.modules[0].classes:
                    cls.methods = [m for m in cls.methods if m.simple_name in include]
            if exclude:
                extracted.modules[0].functions = [f for f in extracted.modules[0].functions if f.simple_name not in exclude]
                for cls in extracted.modules[0].classes:
                    cls.methods = [m for m in cls.methods if m.simple_name not in exclude]

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
        """Build the MCP server (extract, filter, route).

        Returns:
            The configured FastMCP server instance.
        """
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
        for override in self._config.tools:
            for tool in self._registry.get_all_tools():
                qual_name = tool.extracted_obj.qualified_name if tool.extracted_obj else tool.name
                if override.function == qual_name or override.function == tool.name:
                    if override.tests:
                        tool.tests.extend(override.tests)
                    break

        # Step 3: Build router + server
        self._router = NamespaceRouter(
            config=self._config,
            instance_manager=self._instance_manager,
        )
        self._server = self._router.build_server(self._registry)

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
        """Log a human-readable test report."""
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

    def run(self) -> None:
        """Build and run the server.

        Blocks until the server is stopped.
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
            )
        elif transport == "streamable-http":
            self._server.run(
                transport="streamable-http",
                host=self._config.server.host,
                port=self._config.server.port,
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

    def _resolve_source_root(self) -> Path:
        """Resolve the source root directory from config."""
        if self._config.sources:
            # Use first source path
            source_path = self._config.sources[0].path
            path = Path(source_path)
            if not path.is_absolute():
                path = Path.cwd() / path
            return path.resolve()

        return Path.cwd()

    def _get_exclude_patterns(self) -> list[str]:
        """Get exclude patterns from config."""
        if self._config.sources:
            return self._config.sources[0].exclude
        return ["test_*", "*_test.py", "conftest.py"]
