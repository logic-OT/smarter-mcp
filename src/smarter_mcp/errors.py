"""
Structured error types and response formatting for Smarter-MCP.

Tools return JSON-serialised error payloads instead of re-raising, so
agents receive readable, structured output and the server stays alive.

Security note (H4): full tracebacks are NEVER included in client-facing
error payloads — they are logged server-side only (with exc_info=True so
the log handler records the full stack).  A ``debug_include_traceback``
flag exists for development environments and must be explicitly enabled.
"""

from __future__ import annotations

import json
import logging
import traceback
from typing import Literal

logger = logging.getLogger(__name__)


class SmarterMCPError(Exception):
    """Base class for all Smarter-MCP errors."""


class CoercionError(SmarterMCPError):
    """Raised when an input argument cannot be coerced to the expected type."""


class ToolExecutionError(SmarterMCPError):
    """Wraps an internal failure that occurred inside a tool's implementation."""


ErrorType = Literal["coercion_error", "execution_error"]


def format_error_response(
    tool_name: str,
    error: Exception,
    *,
    include_traceback: bool = False,
) -> str:
    """Serialise an exception into a JSON string suitable for returning to an agent.

    Client-facing payload contains only the sanitised message and error type.
    The full traceback is logged at ERROR level on the server side so it is
    available for debugging without being disclosed to callers.

    Args:
        tool_name: The registered name of the tool that failed.
        error: The caught exception.
        include_traceback: When True, append the full traceback to the
            response ``details`` field.  **Enable only in development.**

    Returns:
        A JSON string with keys: error, isError, error_type, tool, message
        (and optionally details when include_traceback is True).
    """
    if isinstance(error, CoercionError):
        error_type: ErrorType = "coercion_error"
    else:
        error_type = "execution_error"

    cause = error.__cause__ if error.__cause__ is not None else error

    # Always log the full traceback server-side (never send to client by default).
    tb_text = "".join(
        traceback.format_exception(type(cause), cause, cause.__traceback__)
    ).strip()
    logger.error(
        "Tool '%s' [%s]: %s\n%s",
        tool_name,
        error_type,
        error,
        tb_text,
    )

    # Sanitised client payload: no file paths, no stack frames.
    payload: dict = {
        "error": True,
        "isError": True,
        "error_type": error_type,
        "tool": tool_name,
        "message": str(error),
    }

    if include_traceback:
        payload["details"] = tb_text

    return json.dumps(payload)
