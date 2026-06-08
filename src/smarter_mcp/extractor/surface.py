"""
Surface Extractor — dual-pass AST + inspect engine.

Pass 1 (AST): Static analysis. No imports, no side effects. Parses source
files to extract functions, classes, methods, properties, and signatures.

Pass 2 (inspect): Runtime analysis. Imports modules to get accurate signatures
from functools.wraps, descriptors, metaclass-generated methods, etc.
Falls back to AST-only results if import fails.

Usage:
    extractor = SurfaceExtractor(source_root="/path/to/mylib")
    result = extractor.extract()
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import logging
import os
import sys
import textwrap
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from .cache import ExtractionCache, cache_enabled
from .docstrings import parse_docstring
from .models import (
    MISSING,
    CallableKind,
    ExtractedCallable,
    ExtractedClass,
    ExtractedModule,
    ExtractedParam,
    ExtractionResult,
    ParamKind,
)
from .type_inference import infer_param_type_from_default, infer_return_type

logger = logging.getLogger(__name__)

# Guards mutation of the global ``sys.path`` during runtime imports. ``sys.path``
# is process-global, so any code that temporarily prepends to it (the inspect
# pass, implementation resolution) must serialize to stay correct if extraction
# is ever parallelized (e.g. a ThreadPoolExecutor across files).
_SYS_PATH_LOCK = threading.Lock()


# Directory names that must never be scanned — virtual environments,
# installed dependencies, VCS metadata, caches, and build artifacts.
# Any dependency/env folder named like one of these is pruned outright.
_EXCLUDED_DIR_NAMES = frozenset({
    "__pycache__",
    "site-packages",
    "dist-packages",
    "node_modules",
    ".git", ".hg", ".svn",
    ".tox", ".nox",
    ".venv", "venv", "env", ".env", "virtualenv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache",
    "build", "dist", ".eggs",
})

# If a single scan would cover more than this many files, emit a loud
# warning — it almost always means source_root was not scoped and the
# whole tree (often a checkout root) is being walked.
_SCAN_FILE_WARN_THRESHOLD = 500


def _is_pruned_dir(name: str) -> bool:
    """Whether a directory should be skipped during discovery.

    Catches known env/cache/build folders, any hidden (dot) directory, and
    Python package metadata folders (``*.egg-info`` / ``*.dist-info``).
    """
    if name in _EXCLUDED_DIR_NAMES:
        return True
    if name.startswith("."):
        return True
    if name.endswith(".egg-info") or name.endswith(".dist-info"):
        return True
    return False


# ──────────────────────────────────────────────────────────────────────
# AST helpers
# ──────────────────────────────────────────────────────────────────────

def _annotation_to_str(node: ast.expr | None) -> str | None:
    """Convert an AST annotation node to a string representation."""
    if node is None:
        return None
    return ast.unparse(node)


def _default_to_value(node: ast.expr) -> Any:
    """Convert an AST default value node to a Python value, if literal."""
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        # Non-literal default — store as string representation
        return ast.unparse(node)


def _get_decorator_names(decorator_list: list[ast.expr]) -> list[str]:
    """Extract decorator names from AST decorator nodes."""
    names = []
    for dec in decorator_list:
        if isinstance(dec, ast.Name):
            names.append(dec.id)
        elif isinstance(dec, ast.Attribute):
            names.append(ast.unparse(dec))
        elif isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name):
                names.append(dec.func.id)
            elif isinstance(dec.func, ast.Attribute):
                names.append(ast.unparse(dec.func))
            else:
                names.append(ast.unparse(dec))
        else:
            names.append(ast.unparse(dec))
    return names


def _get_docstring(node: ast.FunctionDef | ast.ClassDef | ast.Module) -> str | None:
    """Extract the docstring from an AST node."""
    return ast.get_docstring(node)


def _ast_param_kind(arg_name: str, func_node: ast.FunctionDef) -> ParamKind:
    """Determine the ParamKind for a function argument from AST."""
    args = func_node.args

    # Check positional-only
    if arg_name in [a.arg for a in args.posonlyargs]:
        return ParamKind.POSITIONAL_ONLY

    # Check keyword-only
    if arg_name in [a.arg for a in args.kwonlyargs]:
        return ParamKind.KEYWORD_ONLY

    # Check *args
    if args.vararg and args.vararg.arg == arg_name:
        return ParamKind.VAR_POSITIONAL

    # Check **kwargs
    if args.kwarg and args.kwarg.arg == arg_name:
        return ParamKind.VAR_KEYWORD

    return ParamKind.POSITIONAL_OR_KEYWORD


def _detect_callable_kind(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    parent_class: ast.ClassDef | None,
) -> CallableKind:
    """Determine the kind of callable from AST context."""
    if parent_class is None:
        return CallableKind.FUNCTION

    decorator_names = _get_decorator_names(func_node.decorator_list)

    if "property" in decorator_names or any(d.endswith(".setter") for d in decorator_names):
        return CallableKind.PROPERTY
    if "classmethod" in decorator_names:
        return CallableKind.CLASSMETHOD
    if "staticmethod" in decorator_names:
        return CallableKind.STATICMETHOD

    return CallableKind.METHOD


# ──────────────────────────────────────────────────────────────────────
# AST Pass
# ──────────────────────────────────────────────────────────────────────

def _extract_params_from_ast(func_node: ast.FunctionDef) -> list[ExtractedParam]:
    """Extract parameters from an AST function definition."""
    params: list[ExtractedParam] = []
    args = func_node.args

    # Build default mapping: defaults align right-to-left with regular args
    regular_args = args.posonlyargs + args.args
    num_no_default = len(regular_args) - len(args.defaults)
    regular_defaults: list[ast.expr | None] = [None] * num_no_default + list(args.defaults)

    # Positional-only and regular args
    for i, arg in enumerate(regular_args):
        default_node = regular_defaults[i]
        params.append(
            ExtractedParam(
                name=arg.arg,
                annotation=_annotation_to_str(arg.annotation),
                default=_default_to_value(default_node) if default_node else MISSING,
                kind=_ast_param_kind(arg.arg, func_node),
            )
        )

    # *args
    if args.vararg:
        params.append(
            ExtractedParam(
                name=args.vararg.arg,
                annotation=_annotation_to_str(args.vararg.annotation),
                kind=ParamKind.VAR_POSITIONAL,
            )
        )

    # Keyword-only args
    for i, arg in enumerate(args.kwonlyargs):
        default_node = args.kw_defaults[i]
        params.append(
            ExtractedParam(
                name=arg.arg,
                annotation=_annotation_to_str(arg.annotation),
                default=_default_to_value(default_node) if default_node else MISSING,
                kind=ParamKind.KEYWORD_ONLY,
            )
        )

    # **kwargs
    if args.kwarg:
        params.append(
            ExtractedParam(
                name=args.kwarg.arg,
                annotation=_annotation_to_str(args.kwarg.annotation),
                kind=ParamKind.VAR_KEYWORD,
            )
        )

    return params


def _extract_function_ast(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    module_path: str,
    module_name: str,
    parent_class: ast.ClassDef | None = None,
) -> ExtractedCallable:
    """Extract a single callable from an AST function definition."""
    kind = _detect_callable_kind(func_node, parent_class)
    class_name = parent_class.name if parent_class else None

    if class_name:
        qualified = f"{module_name}.{class_name}.{func_node.name}"
    else:
        qualified = f"{module_name}.{func_node.name}"

    params = _extract_params_from_ast(func_node)
    has_variadic = any(p.is_variadic for p in params)

    return ExtractedCallable(
        qualified_name=qualified,
        kind=kind,
        module_path=module_path,
        class_name=class_name,
        is_async=isinstance(func_node, ast.AsyncFunctionDef),
        parameters=params,
        return_type=_annotation_to_str(func_node.returns),
        docstring=_get_docstring(func_node),
        is_inherited=False,  # AST pass can't know this — inspect pass resolves it
        has_variadic=has_variadic,
        decorators=_get_decorator_names(func_node.decorator_list),
        source_lines=(func_node.lineno, func_node.end_lineno or func_node.lineno),
    )


def _extract_class_ast(
    class_node: ast.ClassDef,
    module_path: str,
    module_name: str,
) -> ExtractedClass:
    """Extract a class and all its methods from AST."""
    methods: list[ExtractedCallable] = []
    properties: list[ExtractedCallable] = []
    init_params: list[ExtractedParam] = []

    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            extracted = _extract_function_ast(node, module_path, module_name, class_node)

            if extracted.kind == CallableKind.PROPERTY:
                # Only capture the getter, not setters/deleters
                decorator_names = _get_decorator_names(node.decorator_list)
                is_setter = any(d.endswith(".setter") for d in decorator_names)
                is_deleter = any(d.endswith(".deleter") for d in decorator_names)
                if not is_setter and not is_deleter:
                    properties.append(extracted)
            elif node.name == "__init__":
                # Capture init params for instance configuration
                init_params = extracted.non_self_params
            elif not node.name.startswith("_"):
                methods.append(extracted)
            elif node.name.startswith("_") and not node.name.startswith("__"):
                # Single underscore — private. We keep it but mark it.
                methods.append(extracted)

    bases = []
    for base in class_node.bases:
        bases.append(ast.unparse(base))

    return ExtractedClass(
        name=class_node.name,
        qualified_name=f"{module_name}.{class_node.name}",
        module_path=module_path,
        docstring=_get_docstring(class_node),
        bases=bases,
        methods=methods,
        properties=properties,
        init_params=init_params,
        decorators=_get_decorator_names(class_node.decorator_list),
        source_lines=(class_node.lineno, class_node.end_lineno or class_node.lineno),
    )


def _extract_module_ast(
    tree: ast.Module,
    module_path: str,
    module_name: str,
) -> ExtractedModule:
    """Extract all callables from a pre-parsed module AST."""
    functions: list[ExtractedCallable] = []
    classes: list[ExtractedClass] = []

    # Check for __all__
    all_exports = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    try:
                        all_exports = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        pass

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(_extract_function_ast(node, module_path, module_name))
        elif isinstance(node, ast.ClassDef):
            classes.append(_extract_class_ast(node, module_path, module_name))

    return ExtractedModule(
        module_path=module_path,
        module_name=module_name,
        functions=functions,
        classes=classes,
        docstring=_get_docstring(tree),
        all_exports=all_exports,
    )


# ──────────────────────────────────────────────────────────────────────
# Inspect Pass
# ──────────────────────────────────────────────────────────────────────

_INSPECT_PARAM_KIND_MAP = {
    inspect.Parameter.POSITIONAL_ONLY: ParamKind.POSITIONAL_ONLY,
    inspect.Parameter.POSITIONAL_OR_KEYWORD: ParamKind.POSITIONAL_OR_KEYWORD,
    inspect.Parameter.KEYWORD_ONLY: ParamKind.KEYWORD_ONLY,
    inspect.Parameter.VAR_POSITIONAL: ParamKind.VAR_POSITIONAL,
    inspect.Parameter.VAR_KEYWORD: ParamKind.VAR_KEYWORD,
}


def _import_module_safe(module_name: str, source_root: str) -> Any | None:
    """Import a module by name, returning None if import fails.

    Temporarily adds source_root to sys.path so local modules resolve. The
    mutation is serialized via ``_SYS_PATH_LOCK`` since ``sys.path`` is global.
    """
    with _SYS_PATH_LOCK:
        original_path = sys.path.copy()
        try:
            if source_root not in sys.path:
                sys.path.insert(0, source_root)
            return importlib.import_module(module_name)
        except Exception as exc:
            logger.debug("Failed to import %s: %s", module_name, exc)
            return None
        finally:
            sys.path = original_path


def _enrich_param_from_inspect(
    ast_param: ExtractedParam,
    inspect_param: inspect.Parameter,
) -> ExtractedParam:
    """Merge inspect data into an AST-extracted parameter."""
    # Prefer inspect annotation (handles functools.wraps, descriptors)
    annotation = ast_param.annotation
    if inspect_param.annotation is not inspect.Parameter.empty:
        try:
            annotation = (
                inspect_param.annotation.__name__
                if hasattr(inspect_param.annotation, "__name__")
                else str(inspect_param.annotation)
            )
        except Exception:
            pass

    # Prefer inspect default
    default = ast_param.default
    if inspect_param.default is not inspect.Parameter.empty:
        default = inspect_param.default

    return replace(
        ast_param,
        annotation=annotation,
        default=default,
        kind=_INSPECT_PARAM_KIND_MAP.get(inspect_param.kind, ast_param.kind),
    )


def _enrich_callable_from_inspect(
    extracted: ExtractedCallable,
    runtime_obj: Any,
) -> ExtractedCallable:
    """Enrich an AST-extracted callable with runtime inspect data."""
    try:
        sig = inspect.signature(runtime_obj)
    except (ValueError, TypeError):
        return extracted

    # Build a map of inspect params for matching
    inspect_params = dict(sig.parameters)

    enriched_params = []
    for ast_param in extracted.parameters:
        if ast_param.name in inspect_params:
            enriched_params.append(
                _enrich_param_from_inspect(ast_param, inspect_params[ast_param.name])
            )
        else:
            enriched_params.append(ast_param)

    # Return annotation from inspect
    return_type = extracted.return_type
    if sig.return_annotation is not inspect.Signature.empty:
        try:
            return_type = (
                sig.return_annotation.__name__
                if hasattr(sig.return_annotation, "__name__")
                else str(sig.return_annotation)
            )
        except Exception:
            pass

    return replace(
        extracted,
        parameters=enriched_params,
        return_type=return_type,
        docstring=extracted.docstring or inspect.getdoc(runtime_obj),
    )


def _enrich_class_from_inspect(
    extracted_class: ExtractedClass,
    runtime_class: type,
) -> ExtractedClass:
    """Enrich class with inspect data and detect inherited methods."""
    enriched_methods = []
    for method in extracted_class.methods:
        method_name = method.simple_name
        runtime_method = getattr(runtime_class, method_name, None)
        if runtime_method is not None:
            enriched = _enrich_callable_from_inspect(method, runtime_method)
            # Detect inheritance: check if method is defined in this class or inherited
            for base in runtime_class.__mro__[1:]:
                if method_name in base.__dict__:
                    enriched = replace(
                        enriched,
                        is_inherited=method_name not in runtime_class.__dict__,
                    )
                    break
            enriched_methods.append(enriched)
        else:
            enriched_methods.append(method)

    # Enrich init params
    enriched_init_params = extracted_class.init_params
    init_method = getattr(runtime_class, "__init__", None)
    if init_method:
        try:
            sig = inspect.signature(init_method)
            inspect_params = dict(sig.parameters)
            enriched_init_params = []
            for ast_param in extracted_class.init_params:
                if ast_param.name in inspect_params:
                    enriched_init_params.append(
                        _enrich_param_from_inspect(ast_param, inspect_params[ast_param.name])
                    )
                else:
                    enriched_init_params.append(ast_param)
        except (ValueError, TypeError):
            pass

    return replace(
        extracted_class,
        docstring=extracted_class.docstring or inspect.getdoc(runtime_class),
        methods=enriched_methods,
        init_params=enriched_init_params,
    )


def _enrich_module_from_inspect(
    extracted: ExtractedModule,
    source_root: str,
) -> ExtractedModule:
    """Enrich an AST-extracted module with runtime inspect data."""
    runtime_module = _import_module_safe(extracted.module_name, source_root)
    if runtime_module is None:
        return extracted

    # Enrich functions
    enriched_functions = []
    for func in extracted.functions:
        runtime_func = getattr(runtime_module, func.simple_name, None)
        if runtime_func is not None:
            enriched_functions.append(_enrich_callable_from_inspect(func, runtime_func))
        else:
            enriched_functions.append(func)

    # Enrich classes
    enriched_classes = []
    for cls in extracted.classes:
        runtime_class = getattr(runtime_module, cls.name, None)
        if runtime_class is not None and isinstance(runtime_class, type):
            enriched_classes.append(_enrich_class_from_inspect(cls, runtime_class))
        else:
            enriched_classes.append(cls)

    return replace(
        extracted,
        functions=enriched_functions,
        classes=enriched_classes,
        docstring=extracted.docstring or getattr(runtime_module, "__doc__", None),
    )


# ──────────────────────────────────────────────────────────────────────
# Post-processing: docstring enrichment + type inference
# ──────────────────────────────────────────────────────────────────────

def _enrich_with_docstrings(module: ExtractedModule) -> ExtractedModule:
    """Parse docstrings and attach per-parameter descriptions."""

    def _enrich_callable_docstring(c: ExtractedCallable) -> ExtractedCallable:
        if not c.docstring:
            return c
        parsed = parse_docstring(c.docstring)
        enriched_params = []
        for p in c.parameters:
            desc = parsed.params.get(p.name)
            enriched_params.append(
                replace(
                    p,
                    annotation=p.annotation or parsed.param_types.get(p.name),
                    description=desc or p.description,
                )
            )
        return replace(
            c,
            parameters=enriched_params,
            return_type=c.return_type or parsed.returns_type,
        )

    functions = [_enrich_callable_docstring(f) for f in module.functions]
    classes = [
        replace(cls, methods=[_enrich_callable_docstring(m) for m in cls.methods])
        for cls in module.classes
    ]

    return replace(module, functions=functions, classes=classes)


def _enrich_with_type_inference(
    module: ExtractedModule,
    tree: ast.Module,
) -> ExtractedModule:
    """Infer types for unannotated parameters using defaults and return statements.

    Reuses the already-parsed ``tree`` (no second ``ast.parse``).
    """

    def _enrich_callable_types(c: ExtractedCallable) -> ExtractedCallable:
        enriched_params = []
        for p in c.parameters:
            inferred = p.inferred_type
            if not p.annotation and not inferred:
                inferred = infer_param_type_from_default(p.default)
            enriched_params.append(replace(p, inferred_type=inferred or p.inferred_type))

        # Infer return type if not annotated
        return_type = c.return_type
        if not return_type:
            return_type = infer_return_type(tree, c.simple_name, c.class_name)

        return replace(c, parameters=enriched_params, return_type=return_type)

    functions = [_enrich_callable_types(f) for f in module.functions]
    classes = [
        replace(cls, methods=[_enrich_callable_types(m) for m in cls.methods])
        for cls in module.classes
    ]

    return replace(module, functions=functions, classes=classes)


# ──────────────────────────────────────────────────────────────────────
# Main Extractor Class
# ──────────────────────────────────────────────────────────────────────

class SurfaceExtractor:
    """Extracts all Python surfaces from a source tree.

    Args:
        source_root: Absolute path to the root of the Python source tree.
        use_inspect: Whether to perform the inspect pass (requires importing modules).
        exclude_patterns: Glob patterns for files to skip (e.g., "test_*", "*_test.py").
        use_cache: Enable the disk extraction cache (off by default). Also honors
            the SMARTER_MCP_EXTRACTION_CACHE / SMARTER_MCP_NO_CACHE env vars.
        cache_dir: Where to store cache entries (defaults to .smarter-mcp/extraction-cache).
    """

    def __init__(
        self,
        source_root: str | Path,
        use_inspect: bool = True,
        exclude_patterns: list[str] | None = None,
        use_cache: bool = False,
        cache_dir: str | Path | None = None,
    ):
        self.source_root = Path(source_root).resolve()
        self.use_inspect = use_inspect
        self.exclude_patterns = exclude_patterns or ["test_*", "*_test.py", "conftest.py"]

        self._cache: ExtractionCache | None = (
            ExtractionCache(cache_dir) if cache_enabled(use_cache) else None
        )

    def extract(self) -> ExtractionResult:
        """Run extraction on all Python files under source_root."""
        result = ExtractionResult(source_root=str(self.source_root))
        python_files = self._discover_files()

        for file_path in python_files:
            try:
                module = self._extract_file(file_path)
                if module.tool_count > 0 or module.resource_count > 0:
                    result.modules.append(module)
            except SyntaxError as e:
                result.errors.append(f"Syntax error in {file_path}: {e}")
            except Exception as e:
                result.warnings.append(f"Failed to extract {file_path}: {e}")

        return result

    def extract_file(self, file_path: str | Path) -> ExtractedModule:
        """Extract a single Python file."""
        return self._extract_file(Path(file_path).resolve())

    def extract_source(
        self,
        source: str,
        module_path: str = "<string>",
        module_name: str = "<string>",
    ) -> ExtractedModule:
        """Extract from a source string directly (useful for testing)."""
        tree = ast.parse(source, filename=module_path)
        module = _extract_module_ast(tree, module_path, module_name)
        module = _enrich_with_docstrings(module)
        module = _enrich_with_type_inference(module, tree)
        return module

    def _discover_files(self) -> list[Path]:
        """Find all Python files under source_root.

        Prunes virtual environments, installed dependencies, caches, and build
        artifacts (see ``_is_pruned_dir`` and the ``pyvenv.cfg`` marker), then
        applies the user-supplied exclude globs. Emits a loud warning if the
        result set is unexpectedly large.
        """
        files: list[Path] = []

        for dirpath, dirnames, filenames in os.walk(self.source_root):
            current = Path(dirpath)

            # Definitive virtual-env marker: never descend into a dir tree
            # that contains a pyvenv.cfg (catches venvs with arbitrary names).
            if current != self.source_root and (current / "pyvenv.cfg").exists():
                dirnames[:] = []
                continue

            # Prune excluded subdirectories in place so os.walk skips them.
            dirnames[:] = [d for d in dirnames if not _is_pruned_dir(d)]

            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                py_file = current / filename
                # User-supplied exclude globs (e.g. test_*, *_test.py).
                if self._is_excluded(py_file):
                    continue
                files.append(py_file)

        files.sort()

        if len(files) > _SCAN_FILE_WARN_THRESHOLD:
            logger.warning(
                "\n%s\n"
                "  ⚠️  LARGE SCAN — %d Python files discovered under:\n"
                "      %s\n\n"
                "  This usually means the scan root is too broad (e.g. a whole\n"
                "  project or home directory). Set 'source_root' to your package,\n"
                "  or define 'sources' in a manifest, to scan only your code.\n"
                "%s",
                "=" * 72,
                len(files),
                self.source_root,
                "=" * 72,
            )

        return files

    def _is_excluded(self, file_path: Path) -> bool:
        """Check if a file matches any exclude pattern."""
        for pattern in self.exclude_patterns:
            if file_path.match(pattern):
                return True
        return False

    def _file_to_module_name(self, file_path: Path) -> str:
        """Convert a file path to a dotted module name.

        /source_root/mylib/db/client.py → mylib.db.client
        /source_root/mylib/__init__.py → mylib
        """
        relative = file_path.relative_to(self.source_root)
        parts = list(relative.parts)

        # Remove .py extension
        if parts[-1].endswith(".py"):
            parts[-1] = parts[-1][:-3]

        # __init__ maps to the package itself
        if parts[-1] == "__init__":
            parts = parts[:-1]

        return ".".join(parts) if parts else relative.stem

    def _extract_file(self, file_path: Path) -> ExtractedModule:
        """Extract a single file through both passes (cached by content hash)."""
        source = file_path.read_text(encoding="utf-8")
        relative_path = str(file_path.relative_to(self.source_root))
        module_name = self._file_to_module_name(file_path)

        if self._cache is not None:
            cached = self._cache.get(source, module_name, self.use_inspect)
            if cached is not None:
                logger.debug("Extraction cache hit: %s", relative_path)
                return cached
            module = self._extract_from_source(source, relative_path, module_name)
            self._cache.put(source, module_name, self.use_inspect, module)
            return module

        return self._extract_from_source(source, relative_path, module_name)

    def _extract_from_source(
        self, source: str, relative_path: str, module_name: str
    ) -> ExtractedModule:
        """Run both passes on a source string (parses the AST exactly once)."""
        # Pass 1: AST extraction (single parse, reused by type inference)
        tree = ast.parse(source, filename=relative_path)
        module = _extract_module_ast(tree, relative_path, module_name)

        # Post-process: docstring enrichment
        module = _enrich_with_docstrings(module)

        # Post-process: type inference (reuses the parsed tree)
        module = _enrich_with_type_inference(module, tree)

        # Pass 2: inspect enrichment (optional)
        if self.use_inspect:
            module = _enrich_module_from_inspect(module, str(self.source_root))

        return module
