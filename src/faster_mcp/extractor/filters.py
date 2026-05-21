"""
Exposure filters — decides which extracted callables become MCP tools.

Applies rules from the manifest config to the raw extraction results.
Handles: private functions, inherited methods, variadic signatures,
unannotated callables, explicit tool overrides, and __all__ filtering.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .models import (
    CallableKind,
    ExtractedCallable,
    ExtractedClass,
    ExtractedModule,
    ExtractionResult,
)

logger = logging.getLogger(__name__)


class VariadicPolicy(str, Enum):
    """How to handle functions with *args/**kwargs."""
    SKIP = "skip"       # Silently skip
    WARN = "warn"       # Skip with warning
    EXPOSE = "expose"   # Expose anyway (risky)


class UnannotatedPolicy(str, Enum):
    """How to handle functions with no type annotations."""
    EXPOSE = "expose"   # Expose with inferred types
    WARN = "warn"       # Expose with warning
    SKIP = "skip"       # Silently skip


@dataclass
class ExposureRules:
    """Configuration for what gets exposed as MCP tools."""

    include_private: bool = False
    """Include functions/methods starting with _ (single underscore)."""

    include_dunder: bool = False
    """Include dunder methods (__init__, __str__, etc.)."""

    include_inherited: bool = False
    """Include methods inherited from base classes."""

    include_properties: bool = True
    """Map @property to MCP resources."""

    variadic_policy: VariadicPolicy = VariadicPolicy.WARN
    """How to handle *args/**kwargs."""

    unannotated_policy: UnannotatedPolicy = UnannotatedPolicy.EXPOSE
    """How to handle unannotated callables."""

    respect_all: bool = True
    """If __all__ is defined, only expose listed names."""

    explicit_includes: set[str] = field(default_factory=set)
    """Qualified names to always include, regardless of other rules."""

    explicit_excludes: set[str] = field(default_factory=set)
    """Qualified names to always exclude, regardless of other rules."""


@dataclass
class FilterResult:
    """Result of filtering extraction results."""

    modules: list[ExtractedModule] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)
    warnings: list[str] = field(default_factory=list)


def apply_filters(
    result: ExtractionResult,
    rules: ExposureRules,
) -> FilterResult:
    """Apply exposure rules to extraction results.

    Args:
        result: Raw extraction results from the SurfaceExtractor.
        rules: Exposure rules to apply.

    Returns:
        Filtered results with only exposed callables and skip/warning logs.
    """
    filtered = FilterResult()

    for module in result.modules:
        filtered_module = _filter_module(module, rules, filtered)
        if filtered_module.tool_count > 0 or filtered_module.resource_count > 0:
            filtered.modules.append(filtered_module)

    return filtered


def _filter_module(
    module: ExtractedModule,
    rules: ExposureRules,
    result: FilterResult,
) -> ExtractedModule:
    """Filter a single module's callables."""
    # Determine which names are allowed by __all__
    allowed_names: set[str] | None = None
    if rules.respect_all and module.all_exports is not None:
        allowed_names = set(module.all_exports)

    # Filter functions
    filtered_functions = []
    for func in module.functions:
        verdict, reason = _should_expose(func, rules, allowed_names)
        if verdict:
            filtered_functions.append(func)
        else:
            result.skipped.append((func.qualified_name, reason))

    # Filter classes
    filtered_classes = []
    for cls in module.classes:
        # Check if class itself is allowed
        if allowed_names is not None and cls.name not in allowed_names:
            for method in cls.methods:
                result.skipped.append((method.qualified_name, f"class {cls.name} not in __all__"))
            continue

        # Check explicit class exclusion
        if cls.qualified_name in rules.explicit_excludes:
            for method in cls.methods:
                result.skipped.append((method.qualified_name, "class explicitly excluded"))
            continue

        filtered_methods = []
        for method in cls.methods:
            verdict, reason = _should_expose(method, rules, allowed_names=None)
            if verdict:
                filtered_methods.append(method)
            else:
                result.skipped.append((method.qualified_name, reason))

        filtered_properties = []
        if rules.include_properties:
            filtered_properties = cls.properties

        if filtered_methods or filtered_properties:
            filtered_classes.append(
                ExtractedClass(
                    name=cls.name,
                    qualified_name=cls.qualified_name,
                    module_path=cls.module_path,
                    docstring=cls.docstring,
                    bases=cls.bases,
                    methods=filtered_methods,
                    properties=filtered_properties,
                    init_params=cls.init_params,
                    decorators=cls.decorators,
                    source_lines=cls.source_lines,
                )
            )

    return ExtractedModule(
        module_path=module.module_path,
        module_name=module.module_name,
        functions=filtered_functions,
        classes=filtered_classes,
        docstring=module.docstring,
        all_exports=module.all_exports,
    )


def _should_expose(
    callable: ExtractedCallable,
    rules: ExposureRules,
    allowed_names: set[str] | None,
) -> tuple[bool, str]:
    """Determine if a callable should be exposed as an MCP tool.

    Returns:
        (should_expose, reason_if_not)
    """
    name = callable.simple_name

    # Explicit includes always win
    if callable.qualified_name in rules.explicit_includes:
        return True, ""

    # Explicit excludes always win (after explicit includes)
    if callable.qualified_name in rules.explicit_excludes:
        return False, "explicitly excluded"

    # __all__ filtering (only for module-level functions)
    if allowed_names is not None and callable.class_name is None:
        if name not in allowed_names:
            return False, f"not in __all__"

    # Dunder methods
    if name.startswith("__") and name.endswith("__"):
        if not rules.include_dunder:
            return False, "dunder method"

    # Private methods/functions
    elif name.startswith("_"):
        if not rules.include_private:
            return False, "private (starts with _)"

    # Inherited methods
    if callable.is_inherited and not rules.include_inherited:
        return False, "inherited from base class"

    # Variadic handling
    if callable.has_variadic:
        if rules.variadic_policy == VariadicPolicy.SKIP:
            return False, "has *args/**kwargs (policy: skip)"
        elif rules.variadic_policy == VariadicPolicy.WARN:
            logger.warning(
                "Skipping %s: has *args/**kwargs. Use variadic_policy='expose' to include.",
                callable.qualified_name,
            )
            return False, "has *args/**kwargs (policy: warn)"
        # EXPOSE policy falls through

    # Unannotated handling
    has_annotations = any(p.annotation for p in callable.non_self_params)
    if not has_annotations and callable.non_self_params:
        if rules.unannotated_policy == UnannotatedPolicy.SKIP:
            return False, "no type annotations (policy: skip)"
        elif rules.unannotated_policy == UnannotatedPolicy.WARN:
            logger.warning(
                "Exposing %s without type annotations (using inferred types).",
                callable.qualified_name,
            )

    return True, ""
