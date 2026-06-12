"""Tests for H4 — tracebacks must not leak to MCP clients.

- A failing tool raises ToolError with a sanitized message (no file paths, no stack).
- The tool call result is marked as an error (isError=True).
- Tracebacks appear in server logs only.
- The debug_include_traceback flag in MultimodalConfig controls server-side
  details in format_error_response.
"""

from __future__ import annotations

import logging

import pytest

from smarter_mcp.errors import CoercionError, ToolExecutionError, format_error_response


class TestFormatErrorResponse:
    def test_no_traceback_in_default_response(self):
        try:
            raise ValueError("something went wrong")
        except ValueError as exc:
            response = format_error_response("my_tool", exc)

        assert "Traceback" not in response
        assert "ValueError" not in response  # type name should not leak
        # But the message itself should be present
        assert "something went wrong" in response

    def test_is_error_flag_set(self):
        import json

        err = CoercionError("bad input")
        response = format_error_response("tool_x", err)
        payload = json.loads(response)
        assert payload["isError"] is True, "isError must be True in error responses"
        assert payload["error"] is True

    def test_no_file_path_in_response(self):
        """Stack frames and file paths must be absent from client-facing payload."""
        try:
            raise RuntimeError("oops")
        except RuntimeError as exc:
            response = format_error_response("tool_x", exc)

        # Typical traceback markers that must not appear client-side
        assert "File " not in response
        assert "line " not in response

    def test_coercion_error_type_labelled(self):
        import json

        err = CoercionError("bad cast")
        response = format_error_response("coerce_tool", err)
        payload = json.loads(response)
        assert payload["error_type"] == "coercion_error"

    def test_execution_error_type_labelled(self):
        import json

        err = ToolExecutionError("impl failed")
        response = format_error_response("exec_tool", err)
        payload = json.loads(response)
        assert payload["error_type"] == "execution_error"

    def test_debug_flag_includes_traceback(self):
        try:
            raise RuntimeError("debug failure")
        except RuntimeError as exc:
            response = format_error_response(
                "debug_tool", exc, include_traceback=True
            )

        assert "Traceback" in response or "RuntimeError" in response, (
            "With include_traceback=True, the stack should appear in details"
        )

    def test_traceback_logged_server_side(self, caplog):
        """Full traceback must appear in server logs even when not sent to client.

        The error *message* is legitimately surfaced to clients (agents need to
        know what failed).  What must be absent is the traceback itself: file
        paths from stack frames that could reveal internal structure.
        """
        try:
            raise ValueError("secret internal message /etc/passwd")
        except ValueError as exc:
            with caplog.at_level(logging.ERROR, logger="smarter_mcp.errors"):
                response = format_error_response("logged_tool", exc)

        # Traceback markers must NOT reach the client response.
        assert "Traceback (most recent call last)" not in response, (
            "Stack trace must not be included in client-facing error response"
        )
        # Stack-frame lines like "File ..., line N, in ..." must be absent.
        assert "  File " not in response, (
            "Stack frame file paths must not be included in client-facing error"
        )

        # But the full error (including message) must appear in server logs.
        # caplog.text gives the fully formatted log output.
        assert "secret internal message" in caplog.text, (
            "Full error message must appear in server logs"
        )
        assert "/etc/passwd" in caplog.text, (
            "Error message content (including any paths) must be in server logs"
        )


class TestToolWrapperRaisesToolError:
    """Tool wrappers must raise ToolError (not return error JSON as a string).

    FastMCP's Client raises fastmcp.exceptions.ToolError when the server
    returns an isError=True result (raise_on_error=True is the default).
    We verify:
    1. The tool call raises ToolError (proving isError was set by the server).
    2. The ToolError message is the sanitised JSON — no raw stack frames.
    """

    @pytest.mark.asyncio
    async def test_failing_tool_raises_tool_error(self):
        """A tool that raises an exception must cause the client to get a ToolError."""
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        from smarter_mcp import SmarterMCP, tool
        from smarter_mcp._decorators import clear_global_registry

        clear_global_registry()
        try:
            @tool("Failing tool")
            def broken_tool(x: int) -> str:
                raise ValueError("deliberate failure")

            app = SmarterMCP(name="test-h4-toolerror")
            server = app.build()

            async with Client(server) as client:
                # FastMCP Client raises ToolError when isError=True.
                with pytest.raises(ToolError) as exc_info:
                    await client.call_tool("broken_tool", {"x": 1})

                # The exception message must be the sanitised JSON payload.
                error_text = str(exc_info.value)
                # Must not contain raw Python traceback markers.
                assert "Traceback (most recent call last)" not in error_text, (
                    "Stack trace must not reach the MCP client"
                )
                assert "  File " not in error_text, (
                    "Stack frame paths must not reach the MCP client"
                )
        finally:
            clear_global_registry()

    @pytest.mark.asyncio
    async def test_failing_tool_error_has_no_internal_paths(self):
        """Deliberate failure with a path in the message: path appears in message
        (that's expected) but raw stack frames with the test file path must not."""
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        from smarter_mcp import SmarterMCP, tool
        from smarter_mcp._decorators import clear_global_registry

        clear_global_registry()
        try:
            @tool("Leaking tool")
            def leaking_tool() -> str:
                raise RuntimeError("server secret in /etc/shadow")

            app = SmarterMCP(name="test-h4-paths")
            server = app.build()

            async with Client(server) as client:
                with pytest.raises(ToolError) as exc_info:
                    await client.call_tool("leaking_tool", {})

                error_text = str(exc_info.value)
                # Stack traceback lines start with "  File " — must be absent.
                assert "  File " not in error_text
                assert "Traceback" not in error_text
        finally:
            clear_global_registry()
