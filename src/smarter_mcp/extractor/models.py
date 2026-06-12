"""
Core data models for the extraction engine.

These dataclasses represent the intermediate representation between
raw Python source code and MCP tool/resource registration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# Sentinel for "no default value"
class _MISSING_TYPE:
    """Sentinel indicating a parameter has no default value."""

    def __repr__(self) -> str:
        return "<MISSING>"

    def __bool__(self) -> bool:
        return False


MISSING = _MISSING_TYPE()


class _NON_LITERAL_TYPE:
    """Sentinel indicating a parameter default is non-literal (e.g. datetime.now()).

    Stored by the AST extractor when a default expression cannot be evaluated
    via ``ast.literal_eval``.  Callers must NOT emit this value as a JSON
    schema default or use it for type inference.
    """

    def __repr__(self) -> str:
        return "<NON_LITERAL>"

    def __bool__(self) -> bool:
        return False


NON_LITERAL = _NON_LITERAL_TYPE()


class CallableKind(str, Enum):
    """The kind of callable extracted from source code."""

    FUNCTION = "function"
    METHOD = "method"
    CLASSMETHOD = "classmethod"
    STATICMETHOD = "staticmethod"
    PROPERTY = "property"


class ParamKind(str, Enum):
    """Parameter passing convention."""

    POSITIONAL_ONLY = "positional_only"
    POSITIONAL_OR_KEYWORD = "positional_or_keyword"
    KEYWORD_ONLY = "keyword_only"
    VAR_POSITIONAL = "var_positional"  # *args
    VAR_KEYWORD = "var_keyword"  # **kwargs


class DocstringFormat(str, Enum):
    """Detected docstring format."""

    GOOGLE = "google"
    NUMPY = "numpy"
    SPHINX = "sphinx"
    PLAIN = "plain"


@dataclass
class ExtractedParam:
    """A single parameter extracted from a callable signature."""

    name: str
    annotation: str | None = None
    default: Any = MISSING
    kind: ParamKind = ParamKind.POSITIONAL_OR_KEYWORD
    description: str | None = None
    inferred_type: str | None = None

    @property
    def has_default(self) -> bool:
        """Whether this parameter has a default value."""
        return not isinstance(self.default, _MISSING_TYPE)

    @property
    def is_variadic(self) -> bool:
        """Whether this is *args or **kwargs."""
        return self.kind in (ParamKind.VAR_POSITIONAL, ParamKind.VAR_KEYWORD)

    @property
    def effective_type(self) -> str | None:
        """Best available type: annotation > inferred > None."""
        return self.annotation or self.inferred_type


@dataclass
class ExtractedCallable:
    """A callable (function, method, property) extracted from source code."""

    qualified_name: str
    kind: CallableKind
    module_path: str  # relative to source root
    class_name: str | None = None
    is_async: bool = False
    parameters: list[ExtractedParam] = field(default_factory=list)
    return_type: str | None = None
    docstring: str | None = None
    is_inherited: bool = False
    has_variadic: bool = False
    decorators: list[str] = field(default_factory=list)
    source_lines: tuple[int, int] = (0, 0)

    @property
    def simple_name(self) -> str:
        """The unqualified name (e.g., 'query' from 'mylib.db.Client.query')."""
        return self.qualified_name.rsplit(".", 1)[-1]

    @property
    def tool_name(self) -> str:
        """Default MCP tool name: ClassName_method for methods, function for functions."""
        if self.class_name and self.kind in (CallableKind.METHOD, CallableKind.CLASSMETHOD):
            return f"{self.class_name}_{self.simple_name}"
        return self.simple_name

    @property
    def non_self_params(self) -> list[ExtractedParam]:
        """Parameters excluding the implicit instance/class receiver.

        Only the first positional parameter of METHOD, CLASSMETHOD, and PROPERTY
        callables is stripped — and only when its name is the conventional
        ``self`` or ``cls``.  Free functions (FUNCTION) and static methods
        (STATICMETHOD) are returned unmodified, even if a parameter happens to be
        named ``self`` or ``cls``.
        """
        if self.kind in (CallableKind.METHOD, CallableKind.PROPERTY):
            if self.parameters and self.parameters[0].name == "self":
                return self.parameters[1:]
            return list(self.parameters)
        if self.kind == CallableKind.CLASSMETHOD:
            if self.parameters and self.parameters[0].name == "cls":
                return self.parameters[1:]
            return list(self.parameters)
        # FUNCTION, STATICMETHOD — no implicit receiver
        return list(self.parameters)

    @property
    def non_variadic_params(self) -> list[ExtractedParam]:
        """Parameters excluding *args and **kwargs."""
        return [p for p in self.non_self_params if not p.is_variadic]


@dataclass
class ExtractedClass:
    """A class extracted from source code, containing its methods and properties."""

    name: str
    qualified_name: str
    module_path: str
    docstring: str | None = None
    bases: list[str] = field(default_factory=list)
    methods: list[ExtractedCallable] = field(default_factory=list)
    properties: list[ExtractedCallable] = field(default_factory=list)
    init_params: list[ExtractedParam] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    source_lines: tuple[int, int] = (0, 0)


@dataclass
class ExtractedModule:
    """The complete extraction result for a single Python module."""

    module_path: str  # relative path like "mylib/db/client.py"
    module_name: str  # dotted name like "mylib.db.client"
    functions: list[ExtractedCallable] = field(default_factory=list)
    classes: list[ExtractedClass] = field(default_factory=list)
    docstring: str | None = None
    all_exports: list[str] | None = None  # __all__ if defined

    @property
    def all_callables(self) -> list[ExtractedCallable]:
        """All callables across functions and classes."""
        result = list(self.functions)
        for cls in self.classes:
            result.extend(cls.methods)
            result.extend(cls.properties)
        return result

    @property
    def tool_count(self) -> int:
        """Total number of potential MCP tools."""
        return len(self.functions) + sum(len(c.methods) for c in self.classes)

    @property
    def resource_count(self) -> int:
        """Total number of potential MCP resources (properties)."""
        return sum(len(c.properties) for c in self.classes)


@dataclass
class ExtractionResult:
    """Complete extraction result for an entire codebase."""

    modules: list[ExtractedModule] = field(default_factory=list)
    source_root: str = ""
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_tools(self) -> int:
        return sum(m.tool_count for m in self.modules)

    @property
    def total_resources(self) -> int:
        return sum(m.resource_count for m in self.modules)

    @property
    def total_classes(self) -> int:
        return sum(len(m.classes) for m in self.modules)

    def summary(self) -> str:
        """Human-readable summary of extraction results."""
        lines = [
            f"Extracted from: {self.source_root}",
            f"  Modules: {len(self.modules)}",
            f"  Classes: {self.total_classes}",
            f"  Tools:   {self.total_tools}",
            f"  Resources: {self.total_resources}",
        ]
        if self.warnings:
            lines.append(f"  Warnings: {len(self.warnings)}")
        if self.errors:
            lines.append(f"  Errors: {len(self.errors)}")
        return "\n".join(lines)
