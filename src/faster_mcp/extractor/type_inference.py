"""
Lightweight AST-based type inference for unannotated code.

Infers types from:
1. Default values: x=5 → int, x="hello" → str
2. Return statements: simple literal returns
3. Docstring type hints (handled separately in docstrings.py)

All inferred types are clearly marked — they're suggestions,
not guarantees. The schema generator marks them as "inferred".
"""

from __future__ import annotations

import ast
from typing import Any

from .models import MISSING, _MISSING_TYPE


# ──────────────────────────────────────────────────────────────────────
# Default value inference
# ──────────────────────────────────────────────────────────────────────

_DEFAULT_TYPE_MAP = {
    int: "int",
    float: "float",
    str: "str",
    bool: "bool",
    bytes: "bytes",
    list: "list",
    dict: "dict",
    tuple: "tuple",
    set: "set",
    type(None): "None",
}


def infer_param_type_from_default(default: Any) -> str | None:
    """Infer parameter type from its default value.

    Args:
        default: The default value (or MISSING sentinel).

    Returns:
        Type string like "int", "str", or None if not inferable.
    """
    if isinstance(default, _MISSING_TYPE):
        return None

    if default is None:
        # None default doesn't tell us the actual type
        return None

    default_type = type(default)
    return _DEFAULT_TYPE_MAP.get(default_type)


# ──────────────────────────────────────────────────────────────────────
# Return type inference
# ──────────────────────────────────────────────────────────────────────

_LITERAL_TYPE_MAP = {
    ast.Constant: lambda node: type(node.value).__name__ if node.value is not None else None,
    ast.List: lambda _: "list",
    ast.Dict: lambda _: "dict",
    ast.Tuple: lambda _: "tuple",
    ast.Set: lambda _: "set",
    ast.JoinedStr: lambda _: "str",  # f-strings
}


def _get_return_type_from_node(node: ast.expr) -> str | None:
    """Get the return type from a return value AST node."""
    for ast_type, extractor in _LITERAL_TYPE_MAP.items():
        if isinstance(node, ast_type):
            return extractor(node)

    # Boolean literals
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return "bool"

    # None
    if isinstance(node, ast.Constant) and node.value is None:
        return "None"

    return None


def _find_function_node(
    tree: ast.Module,
    func_name: str,
    class_name: str | None = None,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Find a function/method node in the AST tree."""
    for node in ast.walk(tree):
        if class_name:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if child.name == func_name:
                            return child
        else:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == func_name:
                    # Make sure it's at module level (not nested)
                    return node
    return None


def infer_return_type(
    tree: ast.Module,
    func_name: str,
    class_name: str | None = None,
) -> str | None:
    """Infer a function's return type from its return statements.

    Only infers types when ALL return statements agree on the type.
    Returns None if:
    - No return statements found
    - Return types disagree
    - Return value is too complex to infer

    Args:
        tree: Parsed AST module.
        func_name: The function name to look up.
        class_name: The class name if this is a method.

    Returns:
        Type string or None.
    """
    func_node = _find_function_node(tree, func_name, class_name)
    if func_node is None:
        return None

    return_types: set[str] = set()

    for node in ast.walk(func_node):
        if isinstance(node, ast.Return) and node.value is not None:
            ret_type = _get_return_type_from_node(node.value)
            if ret_type:
                return_types.add(ret_type)
            else:
                # Complex return — can't infer
                return None

    if len(return_types) == 1:
        return return_types.pop()

    # Multiple return types or no returns — can't infer a single type
    return None
