"""
Multi-format docstring parser.

Supports Google, NumPy, and Sphinx/reST docstring formats.
Auto-detects format and extracts per-parameter descriptions,
types, return descriptions, and exception information.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .models import DocstringFormat


@dataclass
class ParsedDocstring:
    """Result of parsing a docstring."""

    summary: str = ""
    description: str = ""
    params: dict[str, str] = field(default_factory=dict)       # name -> description
    param_types: dict[str, str] = field(default_factory=dict)  # name -> type string
    returns: str = ""
    returns_type: str | None = None
    raises: dict[str, str] = field(default_factory=dict)       # exception -> description
    format: DocstringFormat = DocstringFormat.PLAIN


def detect_format(docstring: str) -> DocstringFormat:
    """Detect the docstring format from content patterns."""
    # Google style: "Args:", "Returns:", "Raises:", "Yields:"
    if re.search(r"^\s*(Args|Arguments|Parameters)\s*:", docstring, re.MULTILINE):
        return DocstringFormat.GOOGLE
    if re.search(r"^\s*(Returns|Yields|Raises|Attributes)\s*:", docstring, re.MULTILINE):
        # Could be Google if there's an indented block after
        return DocstringFormat.GOOGLE

    # NumPy style: section headers with "---" underlines
    if re.search(r"^\s*Parameters\s*\n\s*-{3,}", docstring, re.MULTILINE):
        return DocstringFormat.NUMPY
    if re.search(r"^\s*Returns\s*\n\s*-{3,}", docstring, re.MULTILINE):
        return DocstringFormat.NUMPY

    # Sphinx style: ":param name:", ":type name:", ":returns:", ":rtype:"
    if re.search(r"^\s*:(param|type|returns|rtype|raises)\s", docstring, re.MULTILINE):
        return DocstringFormat.SPHINX

    return DocstringFormat.PLAIN


# ──────────────────────────────────────────────────────────────────────
# Google-style parser
# ──────────────────────────────────────────────────────────────────────

_GOOGLE_SECTION_RE = re.compile(
    r"^\s*(Args|Arguments|Parameters|Returns|Yields|Raises|Attributes|Note|Notes|"
    r"Example|Examples|References|Todo|Warnings?)\s*:\s*$",
    re.MULTILINE,
)


def _parse_google(docstring: str) -> ParsedDocstring:
    """Parse a Google-style docstring."""
    result = ParsedDocstring(format=DocstringFormat.GOOGLE)
    lines = docstring.strip().split("\n")

    # Extract summary (first paragraph before any section)
    summary_lines = []
    body_start = 0
    for i, line in enumerate(lines):
        if _GOOGLE_SECTION_RE.match(line):
            body_start = i
            break
        if line.strip() == "" and summary_lines:
            body_start = i + 1
            break
        summary_lines.append(line.strip())
        body_start = i + 1
    result.summary = " ".join(summary_lines)

    # Parse sections
    sections: dict[str, list[str]] = {}
    current_section = None
    for line in lines[body_start:]:
        match = _GOOGLE_SECTION_RE.match(line)
        if match:
            current_section = match.group(1).lower()
            if current_section in ("arguments", "parameters"):
                current_section = "args"
            sections[current_section] = []
        elif current_section is not None:
            sections.setdefault(current_section, []).append(line)

    # Parse Args section
    if "args" in sections:
        _parse_google_params(sections["args"], result)

    # Parse Returns section
    if "returns" in sections:
        returns_text = "\n".join(sections["returns"]).strip()
        # Check for "type: description" pattern
        type_match = re.match(r"^\s*(\w[\w\[\], |]*)\s*:\s*(.+)", returns_text, re.DOTALL)
        if type_match:
            result.returns_type = type_match.group(1).strip()
            result.returns = type_match.group(2).strip()
        else:
            result.returns = returns_text

    # Parse Raises section
    if "raises" in sections:
        _parse_google_raises(sections["raises"], result)

    return result


def _parse_google_params(lines: list[str], result: ParsedDocstring) -> None:
    """Parse parameter entries from Google-style Args section."""
    # Pattern: "name (type): description" or "name: description"
    # Flexible indent — real docstrings can be indented to any depth
    param_re = re.compile(r"^\s+(\w+)\s*(?:\(([^)]+)\))?\s*:\s*(.*)$")
    current_param = None
    current_desc_lines: list[str] = []

    def _flush():
        if current_param:
            result.params[current_param] = " ".join(current_desc_lines).strip()

    for line in lines:
        match = param_re.match(line)
        if match:
            _flush()
            current_param = match.group(1)
            if match.group(2):
                result.param_types[current_param] = match.group(2).strip()
            current_desc_lines = [match.group(3).strip()] if match.group(3).strip() else []
        elif current_param and line.strip():
            current_desc_lines.append(line.strip())
        elif not line.strip() and current_param:
            # Blank line ends param description
            _flush()
            current_param = None
            current_desc_lines = []

    _flush()


def _parse_google_raises(lines: list[str], result: ParsedDocstring) -> None:
    """Parse Raises section from Google-style docstring."""
    raise_re = re.compile(r"^\s+(\w+)\s*:\s*(.*)$")
    for line in lines:
        match = raise_re.match(line)
        if match:
            result.raises[match.group(1)] = match.group(2).strip()


# ──────────────────────────────────────────────────────────────────────
# NumPy-style parser
# ──────────────────────────────────────────────────────────────────────

_NUMPY_SECTION_RE = re.compile(r"^\s*(\w[\w ]*\w)\s*\n\s*-{3,}\s*$", re.MULTILINE)


def _parse_numpy(docstring: str) -> ParsedDocstring:
    """Parse a NumPy-style docstring."""
    result = ParsedDocstring(format=DocstringFormat.NUMPY)

    # Split into sections by header + underline
    parts = _NUMPY_SECTION_RE.split(docstring)

    # First part before any section is summary
    if parts:
        summary_text = parts[0].strip()
        summary_lines = summary_text.split("\n")
        result.summary = summary_lines[0].strip() if summary_lines else ""
        if len(summary_lines) > 2:
            result.description = "\n".join(summary_lines[2:]).strip()

    # Process section pairs (name, content)
    i = 1
    while i < len(parts) - 1:
        section_name = parts[i].strip().lower()
        section_body = parts[i + 1]
        i += 2

        if section_name == "parameters":
            _parse_numpy_params(section_body, result)
        elif section_name == "returns":
            _parse_numpy_returns(section_body, result)
        elif section_name == "raises":
            _parse_numpy_raises(section_body, result)

    return result


def _parse_numpy_params(body: str, result: ParsedDocstring) -> None:
    """Parse NumPy Parameters section.

    Format:
        param_name : type
            Description text.
    """
    # Pattern: "name : type" on its own line (applied to stripped text)
    param_re = re.compile(r"^(\w+)\s*:\s*(.+)?$")
    lines = body.split("\n")
    current_param = None
    current_desc_lines: list[str] = []

    # Detect the base indent level (minimum non-empty indent in the body)
    indents = []
    for line in lines:
        if line.strip():
            indent = len(line) - len(line.lstrip())
            indents.append(indent)
    base_indent = min(indents) if indents else 0

    def _flush():
        if current_param:
            result.params[current_param] = " ".join(current_desc_lines).strip()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        indent = len(line) - len(line.lstrip())

        # Param declarations are at the base indent level
        # Description lines are indented further
        if indent <= base_indent and ":" in stripped:
            match = param_re.match(stripped)
            if match:
                _flush()
                current_param = match.group(1)
                if match.group(2):
                    result.param_types[current_param] = match.group(2).strip()
                current_desc_lines = []
                continue

        # Everything else is a description continuation
        if current_param:
            current_desc_lines.append(stripped)

    _flush()


def _parse_numpy_returns(body: str, result: ParsedDocstring) -> None:
    """Parse NumPy Returns section."""
    lines = body.strip().split("\n")
    if not lines:
        return

    # First non-empty line might be "type" or "name : type"
    first = lines[0].strip()
    if ":" in first:
        parts = first.split(":", 1)
        result.returns_type = parts[0].strip() if parts[0].strip() else None
        result.returns = parts[1].strip()
    else:
        result.returns_type = first
        result.returns = " ".join(l.strip() for l in lines[1:] if l.strip())


def _parse_numpy_raises(body: str, result: ParsedDocstring) -> None:
    """Parse NumPy Raises section."""
    lines = body.strip().split("\n")
    current_exc = None
    for line in lines:
        stripped = line.strip()
        if stripped and not line.startswith(" " * 4):
            current_exc = stripped.rstrip(":")
            result.raises[current_exc] = ""
        elif current_exc and stripped:
            if result.raises[current_exc]:
                result.raises[current_exc] += " " + stripped
            else:
                result.raises[current_exc] = stripped


# ──────────────────────────────────────────────────────────────────────
# Sphinx/reST-style parser
# ──────────────────────────────────────────────────────────────────────

def _parse_sphinx(docstring: str) -> ParsedDocstring:
    """Parse a Sphinx/reST-style docstring."""
    result = ParsedDocstring(format=DocstringFormat.SPHINX)
    lines = docstring.strip().split("\n")

    # Extract summary (lines before first :directive:)
    summary_lines = []
    directive_start = len(lines)
    for i, line in enumerate(lines):
        if re.match(r"^\s*:", line):
            directive_start = i
            break
        summary_lines.append(line.strip())

    result.summary = " ".join(l for l in summary_lines if l).strip()

    # Parse directives
    param_re = re.compile(r"^\s*:param\s+(\w+)\s*:\s*(.*)$")
    type_re = re.compile(r"^\s*:type\s+(\w+)\s*:\s*(.*)$")
    returns_re = re.compile(r"^\s*:returns?\s*:\s*(.*)$")
    rtype_re = re.compile(r"^\s*:rtype\s*:\s*(.*)$")
    raises_re = re.compile(r"^\s*:raises?\s+(\w+)\s*:\s*(.*)$")

    for line in lines[directive_start:]:
        m = param_re.match(line)
        if m:
            result.params[m.group(1)] = m.group(2).strip()
            continue

        m = type_re.match(line)
        if m:
            result.param_types[m.group(1)] = m.group(2).strip()
            continue

        m = returns_re.match(line)
        if m:
            result.returns = m.group(1).strip()
            continue

        m = rtype_re.match(line)
        if m:
            result.returns_type = m.group(1).strip()
            continue

        m = raises_re.match(line)
        if m:
            result.raises[m.group(1)] = m.group(2).strip()
            continue

    return result


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────

def parse_docstring(docstring: str) -> ParsedDocstring:
    """Parse a docstring in any supported format.

    Auto-detects the format and dispatches to the appropriate parser.

    Args:
        docstring: Raw docstring text.

    Returns:
        ParsedDocstring with extracted parameter descriptions, types, etc.
    """
    if not docstring or not docstring.strip():
        return ParsedDocstring()

    fmt = detect_format(docstring)

    if fmt == DocstringFormat.GOOGLE:
        return _parse_google(docstring)
    elif fmt == DocstringFormat.NUMPY:
        return _parse_numpy(docstring)
    elif fmt == DocstringFormat.SPHINX:
        return _parse_sphinx(docstring)
    else:
        # Plain docstring — just extract summary
        lines = docstring.strip().split("\n")
        return ParsedDocstring(
            summary=lines[0].strip(),
            description="\n".join(lines[1:]).strip() if len(lines) > 1 else "",
            format=DocstringFormat.PLAIN,
        )
