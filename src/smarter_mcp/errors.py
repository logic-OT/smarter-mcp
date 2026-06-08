"""
Structured error types and response formatting for Smarter-MCP.

Tools return JSON-serialised error payloads instead of re-raising, so
agents receive readable, structured output and the server stays alive.
"""

from __future__ import annotations

import json
import traceback
from typing import Literal


class SmarterMCPError(Exception):
    """Base class for all Smarter-MCP errors."""


class CoercionError(SmarterMCPError):
    """Raised when an input argument cannot be coerced to the expected type."""


class ToolExecutionError(SmarterMCPError):
    """Wraps an internal failure that occurred inside a tool's implementation."""


ErrorType = Literal["coercion_error", "execution_error"]


def format_error_response(tool_name: str, error: Exception) -> str:
    """Serialise an exception into a JSON string suitable for returning to an agent.

    Args:
        tool_name: The registered name of the tool that failed.
        error: The caught exception.

    Returns:
        A JSON string with keys: error, error_type, tool, message, details.
    """
    if isinstance(error, CoercionError):
        error_type: ErrorType = "coercion_error"
    else:
        error_type = "execution_error"

    cause = error.__cause__ if error.__cause__ is not None else error
    details = "".join(traceback.format_exception(type(cause), cause, cause.__traceback__))

    payload = {
        "error": True,
        "error_type": error_type,
        "tool": tool_name,
        "message": str(error),
        "details": details.strip(),
    }
    return json.dumps(payload)
