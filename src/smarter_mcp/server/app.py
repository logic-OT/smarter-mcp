"""
Main server application — wires extraction, filtering, routing, and instances.

This is the primary entry point for programmatic usage:

    from smarter_mcp.server.app import SmarterMCP

    server = SmarterMCP("my-server", source_root="./mylib")
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

from smarter_mcp._registry import ToolRegistry
from smarter_mcp._testing import TestReport, ToolTestRunner
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
from smarter_mcp.extractor.models import (
    CallableKind,
    ExtractedCallable,
    ExtractedModule,
    ExtractedParam,
    ExtractionResult,
    ParamKind,
)
from smarter_mcp.extractor.surface import _INSPECT_PARAM_KIND_MAP, _SYS_PATH_LOCK, SurfaceExtractor
from smarter_mcp.runtime.instances import InstanceManager
from smarter_mcp.server.router import NamespaceRouter

logger = logging.getLogger(__name__)

# Loopback addresses — binding to anything else without auth is dangerous.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _warn_insecure_bind(server_config: Any) -> None:
    """H1: emit a loud WARNING when the server is bound to a non-loopback
    address with authentication disabled.

    This does NOT prevent startup — operators may have valid reasons (e.g.
    a private LAN with no public exposure) — but the warning is hard to miss.
    """
    host = getattr(server_config, "host", "127.0.0.1")
    auth_enabled = getattr(server_config, "auth_enabled", False)
    if host not in _LOOPBACK_HOSTS and not auth_enabled:
        logger.warning(
            "SECURITY WARNING: Server is binding to %s with auth_enabled=False. "
            "All tools are accessible to anyone on the network without authentication. "
            "Set auth_enabled=True and configure %s, or bind to 127.0.0.1.",
            host,
            getattr(server_config, "auth_keys_env", "SMARTER_MCP_API_KEYS"),
        )


def _fix_package_module_names(
    extraction: ExtractionResult,
    pkg_name: str,
) -> ExtractionResult:
    """Rewrite module names in a package extraction to use dotted import paths.

    When ``SurfaceExtractor`` scans from ``package_dir`` (to avoid walking the
    entire stdlib / project root), the extracted module names are relative to
    the package directory:

    - ``__init__.py`` → module_name ``"__init__"`` (should be ``"json"``)
    - ``decoder.py``  → module_name ``"decoder"``   (should be ``"json.decoder"``)

    This function patches both ``module_name`` and all ``qualified_name`` fields
    so that ``_resolve_implementations`` can call
    ``importlib.import_module("json.decoder")`` and ``merge_extraction`` can
    correctly key impls by ``"json.decoder.JSONDecoder.decode"``.

    C4 fix.
    """
    fixed_modules = []
    for mod_item in extraction.modules:
        old_name = mod_item.module_name
        if old_name in ("__init__", ""):
            new_name = pkg_name
        else:
            new_name = f"{pkg_name}.{old_name}"

        prefix_old = old_name + "." if old_name else ""
        prefix_new = new_name + "."

        def _fix_qname(qname: str) -> str:
            if qname.startswith(prefix_old):
                return prefix_new + qname[len(prefix_old):]
            # Bare name with no prefix (happens when old_name is "")
            if not prefix_old:
                return f"{new_name}.{qname}" if qname else new_name
            return qname

        new_functions = [
            replace(f, qualified_name=_fix_qname(f.qualified_name))
            for f in mod_item.functions
        ]
        new_classes = [
            replace(
                c,
                qualified_name=_fix_qname(c.qualified_name),
                methods=[
                    replace(m, qualified_name=_fix_qname(m.qualified_name))
                    for m in c.methods
                ],
            )
            for c in mod_item.classes
        ]
        fixed_modules.append(replace(
            mod_item,
            module_name=new_name,
            functions=new_functions,
            classes=new_classes,
        ))

    return replace(extraction, modules=fixed_modules)


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
) -> tuple[dict[str, Callable], int, int]:
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
        A 3-tuple of (impls, failed_module_count, skipped_tool_count):
        - impls: dict mapping qualified callable names to Python callable objects.
        - failed_module_count: number of modules that failed to import.
        - skipped_tool_count: total number of tools unavailable due to import failures.
    """
    impls: dict[str, Callable] = {}
    failed_modules = 0
    skipped_tools = 0

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
                    # M6: module import failures are errors, not warnings — the
                    # entire module's tools are silently dropped otherwise.
                    logger.error(
                        "Failed to import module '%s': %s — all tools in this "
                        "module will be unavailable.",
                        module.module_name, e,
                    )
                    failed_modules += 1
                    skipped_tools += len(module.all_callables)
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

    # M6: one end-of-pass summary so operators know the aggregate impact of
    # import failures without having to scan per-module ERROR lines.
    if failed_modules:
        logger.error(
            "%d module(s) failed to import; %d tool(s) skipped.",
            failed_modules, skipped_tools,
        )

    return impls, failed_modules, skipped_tools


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
        Decorator-only (no source discovery):
            >>> server = SmarterMCP("my-server")
            >>> server.run()

        Default file-based auto-discovery:
            >>> server = SmarterMCP("my-server", source_root="./src/my_app")
            >>> server.run()  # Starts SSE server on port 8000

        Explicit manifest-driven server:
            >>> server = SmarterMCP(manifest="smarter-mcp.yaml")
            >>> server.run()

        Programmatic configuration with custom settings:
            >>> server = SmarterMCP(
            ...     "Customer Support API",
            ...     source_root="./src/my_app",
            ...     port=3000,
            ...     transport="sse",
            ...     auth_enabled=True,
            ...     rate_limit_enabled=True,
            ... )
            >>> server.run()
    """

    def __init__(
        self,
        name: str | None = None,
        *,
        source_root: str | Path | None = None,
        manifest: str | Path | None = None,
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
            name: Server name (the first positional argument, matching FastMCP convention).
                Overrides the name in the manifest when both are provided.
                Example: ``SmarterMCP("my-server")``.
            source_root: Keyword-only. File path to the Python source code directory.
                Raises ``ValueError`` if the path is given but does not exist.
                If not specified and no manifest file is found, the server defaults to
                decorator-only registration mode.
            manifest: Keyword-only. File path to a YAML configuration manifest file
                (e.g., ``smarter-mcp.yaml``).
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
        # C3: validate source_root existence before we try to use it so that a
        # missing directory fails loudly instead of silently yielding zero tools.
        if source_root is not None:
            _sr_path = Path(source_root).resolve()
            if not _sr_path.exists():
                raise ValueError(
                    f"source_root does not exist: {_sr_path!r}. "
                    "Create the directory first or pass a valid path."
                )

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
        # C3: name is the first positional parameter (matching FastMCP convention),
        # so SmarterMCP("my-server") correctly sets the server name.
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
        # Accumulates the count of modules that failed to import across all
        # _resolve_implementations calls; surfaced in the /health endpoint.
        self._import_failure_count: int = 0

        # Track which decorator-registered objects this instance has already
        # consumed, so repeated build() calls don't double-register and the
        # global registry is never cleared (clearing it would break sibling
        # SmarterMCP instances in the same process).
        self._registered_decorator_ids: set[int] = set()

        # M4: guard the manifest test-wiring step so a second build() call
        # does not re-extend tool.tests with duplicate cases.
        self._tests_wired: bool = False

        # Wire server.log_level: configure the root Python logger level so the
        # manifest controls verbosity without requiring CLI flags.  An invalid
        # value (e.g. "verbose" instead of "debug") is silently ignored so a
        # typo does not prevent the server from starting.
        _level_name = self._config.server.log_level.upper()
        _level = getattr(logging, _level_name, None)
        if isinstance(_level, int):
            logging.getLogger().setLevel(_level)



    def discover(
        self,
        source_root: str | Path,
        exclude: list[str] | None = None,
        include: list[str] | None = None,
        use_cache: bool = False,
    ) -> SmarterMCP:
        """Scan the filesystem within a source directory to discover and register tools.

        Runs AST extraction followed by runtime inspect passes on the detected modules. Excludes
        common test files and patterns automatically unless customized.

        Args:
            source_root: Root directory containing the python code/packages to inspect.
                Raises ``ValueError`` when the path does not exist (C3 fix).
            exclude: Glob patterns or filenames to skip during AST extraction. Defaults to
                filtering test files like `test_*`, `*_test.py`, and `conftest.py`.
            include: When non-empty, only files matching at least one of these glob patterns
                are scanned.  Wired from ``SourceConfig.include`` in manifest sources.
            use_cache: If True, uses the disk cache to skip unchanged source files during AST
                extraction, speeding up startup times.

        Returns:
            The SmarterMCP instance itself, allowing chained calls.
        """
        path = Path(source_root).resolve()
        # C3a: fail loudly on a nonexistent source_root instead of silently
        # yielding zero tools via os.walk on a missing directory.
        if not path.exists():
            raise ValueError(
                f"source_root does not exist: {path!r}. "
                "Create the directory first or pass a valid path."
            )

        extractor = SurfaceExtractor(
            source_root=path,
            use_inspect=self._use_inspect,
            exclude_patterns=exclude or ["test_*", "*_test.py", "conftest.py"],
            include_patterns=include or [],
            use_cache=use_cache,
        )
        extraction = extractor.extract()

        # C5 / M8: surface extraction errors and warnings instead of silently
        # discarding them, and populate the public extraction_result property.
        failed_modules = len(extraction.errors)
        for err in extraction.errors:
            logger.error("Extraction error: %s", err)
        for warn in extraction.warnings:
            logger.warning("Extraction warning: %s", warn)
        if failed_modules:
            logger.error(
                "Extraction summary for %s: %d file(s) had errors — their tools "
                "are unavailable. Fix the errors above and restart.",
                path, failed_modules,
            )

        # M8: accumulate results so the public property is always populated.
        # Use dataclasses.replace to build a fresh object instead of mutating
        # the previous ExtractionResult's lists in place, which would corrupt
        # any cached reference held by the extractor.
        if self._extraction is None:
            self._extraction = extraction
        else:
            self._extraction = replace(
                self._extraction,
                modules=self._extraction.modules + extraction.modules,
                errors=self._extraction.errors + extraction.errors,
                warnings=self._extraction.warnings + extraction.warnings,
            )

        impls, import_fails, _skipped = _resolve_implementations(extraction, str(path))
        self._import_failure_count += import_fails

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
        """Discover and register tools from an already-imported python module or class.

        Automatically detects:
        - Regular ``.py`` files: AST + inspect extraction.
        - Packages (``__init__.py``): walks all submodules via the normal ``extract()``
          path so dotted module names resolve correctly (C4 fix).
        - Classes (``inspect.isclass``): inspect-only path with proper ``class_name``
          binding (C4 fix).
        - C-extensions / stdlib: inspect-only fallback.

        An explicit ``include=[...]`` list overrides the variadic-skip policy so that
        functions with ``*args``/``**kwargs`` are registered when the user asks for them
        by name (C4 fix).

        Args:
            module: The python module or class to inspect.
            include: Optional whitelist. Only callables with these simple names are added.
                Also bypasses variadic-skip policy for named items.
            exclude: Optional blacklist. Callables with these simple names are skipped.
            namespace: Namespace prefix for the discovered tools. Defaults to the
                module's name.

        Returns:
            The SmarterMCP instance itself, allowing chained calls.
        """
        import inspect as py_inspect
        from dataclasses import replace as dc_replace

        # ── C4b: class path ─────────────────────────────────────────────────
        if py_inspect.isclass(module):
            cls = module
            class_module_name = getattr(cls, "__module__", "") or ""
            class_name = cls.__name__
            extracted_mod = ExtractedModule(module_path="", module_name=class_module_name)
            impls: dict[str, Any] = {}

            for mname, obj in py_inspect.getmembers(cls, predicate=py_inspect.isroutine):
                if include and mname not in include:
                    continue
                if exclude and mname in exclude:
                    continue
                if mname.startswith("_"):
                    continue

                # Build parameter metadata from inspect.signature so downstream
                # schema generation has real type info (not empty list).
                try:
                    sig = py_inspect.signature(getattr(cls, mname))
                    params = []
                    for pname, p in sig.parameters.items():
                        if pname in ("self", "cls"):
                            continue
                        ann = (
                            p.annotation.__name__
                            if hasattr(p.annotation, "__name__")
                            and p.annotation is not py_inspect.Parameter.empty
                            else (
                                str(p.annotation)
                                if p.annotation is not py_inspect.Parameter.empty
                                else None
                            )
                        )
                        params.append(ExtractedParam(
                            name=pname,
                            annotation=ann,
                            kind=_INSPECT_PARAM_KIND_MAP.get(p.kind, ParamKind.POSITIONAL_OR_KEYWORD),
                        ))
                except (ValueError, TypeError):
                    params = []

                qualified = f"{class_module_name}.{class_name}.{mname}"
                meta = ExtractedCallable(
                    qualified_name=qualified,
                    kind=CallableKind.METHOD,
                    module_path="",
                    class_name=class_name,
                    parameters=params,
                )
                extracted_mod.functions.append(meta)
                impls[qualified] = getattr(cls, mname)

            extracted = ExtractionResult(modules=[extracted_mod], source_root="")
            ns_name = namespace or class_name
            self._registry.merge_extraction(extracted, impls, namespace_override=ns_name)
            return self

        source_file = getattr(module, "__file__", None)

        if source_file and source_file.endswith(".py"):
            # ── C4a: package path (module is a package with __init__.py) ─────
            if source_file.endswith("__init__.py"):
                # The module is a package. Scan only the package directory to
                # avoid walking the entire stdlib or project tree (using the
                # parent as source_root would scan everything).
                #
                # Strategy:
                #   1. Extract from package_dir (scans only the package).
                #   2. Module names relative to package_dir are wrong:
                #      "__init__" instead of "json", "decoder" instead of
                #      "json.decoder". Fix them with _fix_package_module_names.
                #   3. Pass parent_dir as sys.path entry so importlib can
                #      resolve "json", "json.decoder", etc.
                package_dir = Path(source_file).parent
                parent_dir = package_dir.parent
                pkg_name = module.__name__

                extractor = SurfaceExtractor(
                    source_root=package_dir,
                    use_inspect=self._use_inspect,
                    exclude_patterns=["test_*", "*_test.py", "conftest.py"],
                )
                extraction = extractor.extract()
                # Fix module names: "__init__" → "json", "decoder" → "json.decoder"
                extraction = _fix_package_module_names(extraction, pkg_name)
                source_root_str = str(parent_dir)
            else:
                # ── Regular single-.py file path ─────────────────────────────
                extractor = SurfaceExtractor(
                    source_root=Path(source_file).parent,
                    use_inspect=self._use_inspect,
                )
                extracted_mod = extractor.extract_file(Path(source_file))
                extraction = ExtractionResult(
                    modules=[extracted_mod],
                    source_root=str(Path(source_file).parent),
                )
                source_root_str = str(Path(source_file).parent)

            impls, import_fails, _skipped = _resolve_implementations(extraction, source_root_str)
            self._import_failure_count += import_fails

            # Apply include/exclude filters across all modules.
            if include or exclude:
                include_set = set(include) if include else None
                exclude_set = set(exclude) if exclude else set()
                new_modules = []
                for mod_item in extraction.modules:
                    new_mod = dc_replace(
                        mod_item,
                        functions=[
                            f for f in mod_item.functions
                            if (include_set is None or f.simple_name in include_set)
                            and f.simple_name not in exclude_set
                        ],
                        classes=[
                            dc_replace(
                                c,
                                methods=[
                                    m for m in c.methods
                                    if (include_set is None or m.simple_name in include_set)
                                    and m.simple_name not in exclude_set
                                ],
                            )
                            for c in mod_item.classes
                        ],
                    )
                    if new_mod.tool_count > 0 or new_mod.resource_count > 0:
                        new_modules.append(new_mod)
                extraction.modules = new_modules

            rules = _exposure_rules_from_config(self._config)

            # C4: let an explicit include=[...] override the variadic-skip policy.
            # Without this, a function like json.loads (which has **kwargs) gets
            # filtered out even when the user explicitly asked for it by name.
            if include:
                include_set = set(include)
                extra_includes: set[str] = set()
                for mod_item in extraction.modules:
                    for fn in mod_item.functions:
                        if fn.simple_name in include_set:
                            extra_includes.add(fn.qualified_name)
                    for cls_item in mod_item.classes:
                        for m in cls_item.methods:
                            if m.simple_name in include_set:
                                extra_includes.add(m.qualified_name)
                rules = dc_replace(
                    rules,
                    explicit_includes=rules.explicit_includes | extra_includes,
                )

            filtered = apply_filters(extraction, rules)
            ns_name = namespace or module.__name__
            self._registry.merge_extraction(filtered, impls, namespace_override=ns_name)

        else:
            # ── Inspect-only fallback for C-extensions / built-ins ──────────
            extracted_mod = ExtractedModule(module_path="", module_name=module.__name__)
            impls = {}
            for mname, obj in py_inspect.getmembers(module, py_inspect.isroutine):
                if include and mname not in include:
                    continue
                if exclude and mname in exclude:
                    continue
                if mname.startswith("_"):
                    continue

                meta = ExtractedCallable(
                    qualified_name=f"{module.__name__}.{mname}",
                    kind=CallableKind.FUNCTION,
                    module_path="",
                )
                extracted_mod.functions.append(meta)
                impls[meta.qualified_name] = obj

            extracted = ExtractionResult(modules=[extracted_mod], source_root="")
            ns_name = namespace or module.__name__.split(".")[-1]
            self._registry.merge_extraction(extracted, impls, namespace_override=ns_name)

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
            get_global_resources,
            get_global_toolkits,
            get_global_tools,
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
                # H14: resolve relative paths against the manifest's directory,
                # not CWD, so `smarter-mcp serve -m /elsewhere/smarter-mcp.yaml`
                # scans the right tree.
                src_path = Path(source.path)
                if not src_path.is_absolute() and self._config.manifest_dir:
                    src_path = Path(self._config.manifest_dir) / src_path
                src_path = src_path.resolve()

                if not src_path.exists():
                    logger.error(
                        "Source path '%s' (resolved: %s) does not exist — skipping.",
                        source.path, src_path,
                    )
                    continue

                self.discover(
                    str(src_path),
                    exclude=source.exclude,
                    include=source.include if source.include else None,
                )

        # Step 2: Wire manifest test cases into the registry.
        # ToolOverride.tests defined in YAML need to be merged into the
        # corresponding RegisteredTool.tests so the test runner can find them.
        # O(1) per override via a single name→tool index (built once).
        # Guard with _tests_wired so a second build() call does not append
        # the same cases again (M4 idempotency fix).
        if self._config.tools and not self._tests_wired:
            tool_index = self._registry.index_by_name()
            for override in self._config.tools:
                tool = tool_index.get(override.function)
                if tool is not None and override.tests:
                    tool.tests.extend(override.tests)
            self._tests_wired = True

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
            assert_auth_keys_present,
            build_auth_provider,
            build_rate_limit_middleware,
        )

        # H7 / A2: fail-closed guard — fires regardless of which public
        # entrypoint (build/http_app/run) is used.
        assert_auth_keys_present(self._config.server)

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
        from smarter_mcp.server.security import _constant_time_key_check, load_api_keys

        _health_ep = HealthEndpoint(
            self._router,
            self._registry,
            extraction_result=self._extraction,
            import_failure_count=self._import_failure_count,
        )
        # A1: SchemaEndpoint gets the ROUTER so it builds from the registered
        # tool surface (respects expose=False, name overrides, etc.).
        _schema_ep = SchemaEndpoint(self._registry, router=self._router)

        _auth_enabled = self._config.server.auth_enabled
        _auth_header = self._config.server.auth_header
        _auth_keys_env = self._config.server.auth_keys_env

        def _is_authenticated(request: Request) -> bool:
            """Return True if the request carries a valid API key."""
            if not _auth_enabled:
                return False
            keys = load_api_keys(_auth_keys_env)
            provided = request.headers.get(_auth_header, "")
            return bool(provided) and _constant_time_key_check(provided, keys)

        @self._server.custom_route("/health", methods=["GET"])
        async def _health_handler(request: Request) -> JSONResponse:
            # H8: unauthenticated callers get only the bare status; full
            # detail (namespaces, counts, version) requires a valid API key.
            return JSONResponse(
                _health_ep.get_health(authenticated=_is_authenticated(request))
            )

        @self._server.custom_route("/mcp/{namespace}/schema", methods=["GET"])
        async def _schema_handler(request: Request) -> JSONResponse:
            ns = request.path_params["namespace"]
            compact = request.query_params.get("compact", "false").lower() == "true"
            result = _schema_ep.get_namespace_schema(ns, compact=compact)
            # H8 / A1: if the namespace was not found, return 404 (not 200
            # with an error key, which agents can't distinguish from success).
            if "error" in result:
                return JSONResponse(result, status_code=404)
            return JSONResponse(result)

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
        _warn_insecure_bind(self._config.server)
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
            _warn_insecure_bind(self._config.server)
            self._server.run(
                transport="sse",
                host=self._config.server.host,
                port=self._config.server.port,
                middleware=self._asgi_middleware(),
            )
        elif transport == "streamable-http":
            _warn_insecure_bind(self._config.server)
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

