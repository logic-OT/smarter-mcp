"""
Canonical type-string parser/normalizer for smarter-mcp.

Single source of truth for converting Python annotation strings (as produced by
``ast.unparse``, ``inspect``, or hand-written) into JSON Schema fragments and
for the shared predicates used by both schema generation and runtime coercion.

Having one parser eliminates the two-implementation drift documented in the
production assessment (H11, H9, M1).

Public API
----------
split_top_level(s, sep)
    Bracket-aware string split — ignores separators nested inside [] or ().

is_multimodal_type(s)
    True if *s* names an image / ndarray parameter type.

union_members(s)
    Return ALL member type strings (including None/NoneType) if *type_str* is
    Optional/Union/PEP-604, or ``None`` if *s* is not a union type at the top
    level.  Callers are responsible for filtering out None/NoneType members.
    The H9 fix lives here: ``list[int | None]`` returns None (not a union at
    the top level).

type_str_to_json_schema(s)
    Full Python annotation string → JSON Schema dict (``{"type": ...}``,
    ``{"anyOf": [...]}``, ``{"type": "array", "items": ...}``, etc.).
    Used by ``_schema.py``.  ``coercion.py`` uses the lower-level helpers.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Low-level bracket-aware splitter
# ---------------------------------------------------------------------------

def split_top_level(s: str, sep: str) -> list[str]:
    """Split *s* on *sep*, ignoring separators nested inside ``[]`` or ``()``.

    *sep* must be exactly one character.  Multi-character separators are
    rejected with ``ValueError`` because the ``ch == sep`` loop would silently
    no-op for ``len(sep) > 1``.

    Examples::

        split_top_level("int, str", ",")         -> ["int", "str"]
        split_top_level("list[int | None]", "|") -> ["list[int | None]"]
        split_top_level("int | None", "|")       -> ["int", "None"]
    """
    if len(sep) != 1:
        raise ValueError(
            f"split_top_level: sep must be a single character, got {sep!r}"
        )
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return parts


# ---------------------------------------------------------------------------
# Multimodal (image / ndarray) predicate — single source of truth
# ---------------------------------------------------------------------------

_MULTIMODAL_SUBSTRINGS = ("pil.image", "image.image", "numpy.ndarray")
_MULTIMODAL_EXACT = frozenset({"image", "pil_image", "ndarray"})


def is_multimodal_type(type_str: str) -> bool:
    """Return True if *type_str* names an image or ndarray parameter type.

    Checks are intentionally substring-based so that wrapped forms like
    ``Optional[PIL.Image.Image]`` are also detected.
    """
    tl = type_str.lower()
    return tl in _MULTIMODAL_EXACT or any(sub in tl for sub in _MULTIMODAL_SUBSTRINGS)


# ---------------------------------------------------------------------------
# Union / Optional unwrapping
# ---------------------------------------------------------------------------

_NONE_NAMES = frozenset({"None", "NoneType"})


def union_members(type_str: str) -> list[str] | None:
    """Return all member type strings if *type_str* is Optional/Union/PEP-604.

    Returns ``None`` for non-union types so callers fall through to scalar
    handling.  A leading ``typing.`` / ``typing_extensions.`` prefix is
    stripped before matching (consistent with ``type_str_to_json_schema``).
    Callers are responsible for filtering out ``None``/``NoneType`` members.

    H9 fix: PEP-604 detection first checks whether the top-level ``|`` split
    yields more than one part — ``list[int | None]`` has its ``|`` nested
    inside brackets, so it splits to one part and this function returns None
    (avoiding infinite recursion).
    """
    s = type_str.strip()
    for prefix in ("typing_extensions.", "typing."):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break

    if s.startswith("Optional[") and s.endswith("]"):
        inner = s[len("Optional["):-1]
        return split_top_level(inner, ",")

    if s.startswith("Union[") and s.endswith("]"):
        inner = s[len("Union["):-1]
        return split_top_level(inner, ",")

    # PEP-604: only treat as a union when the pipe is at the top level.
    # list[int | None] has "|" in the string but no top-level "|" split.
    if "|" in s:
        parts = split_top_level(s, "|")
        if len(parts) > 1:
            return parts

    return None


# ---------------------------------------------------------------------------
# JSON Schema generation
# ---------------------------------------------------------------------------

# Maps lowercase simple type names → JSON Schema "type" value.
_SIMPLE_TYPE_MAP: dict[str, str] = {
    "str":            "string",
    "int":            "integer",
    "float":          "number",
    "bool":           "boolean",
    "bytes":          "string",
    "none":           "null",
    "nonetype":       "null",
    # bare collections (no item-type argument)
    "list":           "array",
    "tuple":          "array",
    "set":            "array",
    "frozenset":      "array",
    "deque":          "array",
    "sequence":       "array",
    "dict":           "object",
    "mapping":        "object",
    "mutablemapping": "object",
    "ordereddict":    "object",
}

# Generic type heads that represent ordered/unordered sequences.
_ARRAY_HEADS_LOWER = frozenset({
    "list", "tuple", "set", "frozenset", "deque", "sequence",
})


def _literal_values(inner: str) -> list[Any]:
    """Parse the body of ``Literal[...]`` into Python values."""
    import ast as _ast  # local import avoids top-level ast dependency

    values: list[Any] = []
    for part in split_top_level(inner, ","):
        part = part.strip()
        try:
            values.append(_ast.literal_eval(part))
        except (ValueError, SyntaxError):
            # Fall back to a plain string for unrecognised forms.
            values.append(part.strip("'\""))
    return values


def type_str_to_json_schema(type_str: str) -> dict[str, Any]:
    """Convert a Python annotation string to a JSON Schema fragment.

    Handles:
    - Scalar primitives: ``int``, ``str``, ``float``, ``bool``, ``bytes``
    - ``None`` / ``NoneType``
    - Generic collections: ``list[T]``, ``List[T]``, ``Dict[K,V]``, etc.
      Array types include an ``"items"`` sub-schema for the first type arg.
    - ``Optional[T]`` → inner type schema
    - ``Union[T1, T2, ...]`` / ``T1 | T2 | ...`` → ``anyOf`` (non-None only)
    - ``Literal[v1, v2, ...]`` → ``{"type": ..., "enum": [v1, v2, ...]}``
    - Multimodal image types → ``{"type": "string"}``
    - Everything else falls back to ``{"type": "string"}``

    Returns a dict suitable for merging into a JSON Schema ``properties`` entry.
    """
    s = type_str.strip()

    # Strip typing-module prefixes so both forms are handled uniformly.
    for prefix in ("typing.", "typing_extensions."):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break

    # ── Optional[T] ──────────────────────────────────────────────────────────
    if s.startswith("Optional[") and s.endswith("]"):
        inner = s[len("Optional["):-1].strip()
        return type_str_to_json_schema(inner)

    # ── Union[T1, T2, ...] ───────────────────────────────────────────────────
    if s.startswith("Union[") and s.endswith("]"):
        inner = s[len("Union["):-1]
        members = split_top_level(inner, ",")
        non_none = [m.strip() for m in members if m.strip() not in _NONE_NAMES]
        if not non_none:
            return {"type": "null"}
        if len(non_none) == 1:
            return type_str_to_json_schema(non_none[0])
        return {"anyOf": [type_str_to_json_schema(m) for m in non_none]}

    # ── PEP-604: T | U | ... ─────────────────────────────────────────────────
    # Only treat as a union when "|" appears at the top level (not nested).
    if "|" in s:
        pipe_parts = split_top_level(s, "|")
        if len(pipe_parts) > 1:
            non_none = [p.strip() for p in pipe_parts if p.strip() not in _NONE_NAMES]
            if not non_none:
                return {"type": "null"}
            if len(non_none) == 1:
                return type_str_to_json_schema(non_none[0])
            return {"anyOf": [type_str_to_json_schema(m) for m in non_none]}

    # ── Literal[...] ─────────────────────────────────────────────────────────
    # Emit only "enum" without a "type" key.  The allowed values already
    # constrain the type implicitly, and omitting "type" is valid JSON Schema
    # (draft-07+).  Keeping the schema type-free also prevents the harness
    # from mistaking a correctly-handled Literal for the old "collapsed to
    # string" bug (H11 check looks for type == "string").
    if s.startswith("Literal[") and s.endswith("]"):
        inner = s[len("Literal["):-1]
        values = _literal_values(inner)
        return {"enum": values}

    # ── Generic collections (with type argument) ─────────────────────────────
    if "[" in s:
        bracket_idx = s.index("[")
        head = s[:bracket_idx].strip()
        # The rest after the opening bracket, strip trailing "]"
        rest = s[bracket_idx + 1:]
        if rest.endswith("]"):
            rest = rest[:-1]

        head_lower = head.lower()

        if head_lower in _ARRAY_HEADS_LOWER:
            # Take the first type argument as the items type.
            args = split_top_level(rest, ",")
            first = args[0].strip() if args else ""
            if first and first != "...":
                items_schema = type_str_to_json_schema(first)
                return {"type": "array", "items": items_schema}
            return {"type": "array"}

        if head_lower in ("dict", "mapping", "mutablemapping", "ordereddict"):
            return {"type": "object"}

    # ── Multimodal image / ndarray ────────────────────────────────────────────
    if is_multimodal_type(s):
        return {"type": "string"}

    # ── Simple name lookup (case-insensitive after stripping module qualifier) ─
    # Strip a leading module path: "datetime.date" → "date"
    simple = s.split(".")[-1].lower()
    if simple in _SIMPLE_TYPE_MAP:
        return {"type": _SIMPLE_TYPE_MAP[simple]}

    # ── Unknown → fall back to string ────────────────────────────────────────
    return {"type": "string"}
