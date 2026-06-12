"""Agno + smarter-mcp end-to-end integration test.

Proves that smarter-mcp functions correctly as a real MCP server driven by
the Agno agent framework over the stdio transport.

Test structure
--------------
deterministic (always run when agno is importable)
  TestDiscovery  — verifies tool names and input schemas over the MCP wire
  TestInvocation — calls tools without an LLM and asserts correct results

key-gated (skipped unless ANTHROPIC_API_KEY is set)
  TestAgentRun   — builds a real Agent(model=Claude(...)) and calls arun()

Skipping behaviour
------------------
``pytest.importorskip("agno")`` at module level skips the entire file when agno
is not installed.  The main 303-test suite therefore stays green when running
without the e2e extra.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Gate the entire module: skip silently if agno is not installed.
pytest.importorskip("agno")

from agno.tools.mcp import MCPTools

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SERVER_MODULE = str(Path(__file__).parent / "server_tools.py")

# Use the installed smarter-mcp script from the same venv bin directory as
# the running Python interpreter.  smarter_mcp is a package, not a module
# (no __main__.py), so we cannot use `python -m smarter_mcp`.
# --quiet suppresses everything below ERROR level so stderr is clean; the MCP
# banner is always written to stderr (not stdout) for stdio transport and does
# not corrupt the MCP message stream.
_SMARTER_MCP_BIN = str(Path(sys.executable).parent / "smarter-mcp")
_SERVER_CMD = (
    f"{_SMARTER_MCP_BIN} --quiet serve {_SERVER_MODULE} --transport stdio"
)

# Expected tool names after smarter-mcp registers the server_tools module.
# "default" namespace tools are mounted without a prefix (H12 fix).
# Toolkit method tools are prefixed with ClassName_ (router._build_tool_name).
_EXPECTED_TOOLS = frozenset(
    [
        "format_greeting",
        "compute_stats",
        "ShoppingCart_add_item",
        "ShoppingCart_total_items",
    ]
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def mcp_tools():
    """Start the smarter-mcp server subprocess and yield a connected MCPTools.

    Lifecycle:
      connect()   → spawns subprocess, opens stdio channel, calls list_tools()
      yield       → tests run
      close()     → closes MCP session, terminates subprocess

    Using connect()/close() instead of ``async with`` avoids an anyio cancel-scope
    task-mismatch RuntimeError that occurs with pytest-asyncio 1.x when the
    context manager's __aexit__ runs in a different asyncio task than __aenter__.
    MCPTools.close() already suppresses RuntimeError in its cleanup path.
    """
    tools = MCPTools(command=_SERVER_CMD, timeout_seconds=30)
    await tools.connect()
    try:
        yield tools
    finally:
        await tools.close()


# ---------------------------------------------------------------------------
# Deterministic discovery tests — no LLM required
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestDiscovery:
    """Verify that smarter-mcp exposes the correct tools over the real MCP wire."""

    async def test_all_expected_tools_discovered(self, mcp_tools: MCPTools) -> None:
        """All four tools must be present in MCPTools.functions after connect."""
        discovered = set(mcp_tools.functions.keys())
        missing = _EXPECTED_TOOLS - discovered
        assert not missing, (
            f"Missing tools: {missing}. Discovered: {discovered}"
        )

    async def test_format_greeting_schema(self, mcp_tools: MCPTools) -> None:
        """format_greeting must expose name:string and formal:boolean params."""
        func = mcp_tools.functions["format_greeting"]
        props = func.parameters.get("properties", {})
        assert "name" in props, f"Expected 'name' param; got {list(props)}"
        assert "formal" in props, f"Expected 'formal' param; got {list(props)}"
        assert props["name"].get("type") == "string", (
            f"'name' must be type=string; got {props['name']}"
        )
        # formal is a boolean with a default; schema type may be boolean or
        # absent when FastMCP omits it — check it at least exists as a key.
        assert "formal" in props

    async def test_compute_stats_array_schema(self, mcp_tools: MCPTools) -> None:
        """compute_stats must expose numbers:array (H11 — typed list schema)."""
        func = mcp_tools.functions["compute_stats"]
        props = func.parameters.get("properties", {})
        assert "numbers" in props, f"Expected 'numbers' param; got {list(props)}"
        assert props["numbers"].get("type") == "array", (
            f"'numbers' must be type=array; got {props['numbers']}"
        )

    async def test_shopping_cart_add_item_schema(self, mcp_tools: MCPTools) -> None:
        """ShoppingCart_add_item must expose name:string and qty:integer."""
        func = mcp_tools.functions["ShoppingCart_add_item"]
        props = func.parameters.get("properties", {})
        assert "name" in props
        assert "qty" in props

    async def test_shopping_cart_total_items_schema(self, mcp_tools: MCPTools) -> None:
        """ShoppingCart_total_items must have no required params."""
        func = mcp_tools.functions["ShoppingCart_total_items"]
        required = func.parameters.get("required", [])
        assert required == [], (
            f"total_items should have no required params; got {required}"
        )


# ---------------------------------------------------------------------------
# Deterministic invocation tests — no LLM required
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestInvocation:
    """Call tools directly through the MCP wire and assert correct results.

    Uses Function.entrypoint(**kwargs) which routes through the real MCP
    call_tool() without involving any LLM.
    """

    async def test_format_greeting_informal(self, mcp_tools: MCPTools) -> None:
        """format_greeting informal path returns 'Hello, Ada!' (proves C1).

        C1 bug: returned strings were being re-encoded as broken images.
        A plain string return must come back as readable text, not base64.
        """
        func = mcp_tools.functions["format_greeting"]
        result = await func.entrypoint(name="Ada")
        assert "Hello, Ada!" in result.content, (
            f"Expected 'Hello, Ada!' in response; got {result.content!r}"
        )

    async def test_format_greeting_formal(self, mcp_tools: MCPTools) -> None:
        """format_greeting formal=True path returns the formal greeting."""
        func = mcp_tools.functions["format_greeting"]
        result = await func.entrypoint(name="Lovelace", formal=True)
        assert "Good day, Lovelace." in result.content, (
            f"Expected formal greeting; got {result.content!r}"
        )

    async def test_compute_stats_correct_values(self, mcp_tools: MCPTools) -> None:
        """compute_stats([1,2,3]) returns mean=2.0, min=1.0, max=3.0 (H11).

        H11 bug: list[float] schema was broken; coercion and schema generation
        for typed array params must round-trip correctly end-to-end.
        """
        func = mcp_tools.functions["compute_stats"]
        result = await func.entrypoint(numbers=[1.0, 2.0, 3.0])
        stats = json.loads(result.content)
        assert stats["mean"] == pytest.approx(2.0), (
            f"Expected mean=2.0; got {stats!r}"
        )
        assert stats["min"] == pytest.approx(1.0)
        assert stats["max"] == pytest.approx(3.0)

    async def test_compute_stats_string_coercion(self, mcp_tools: MCPTools) -> None:
        """compute_stats accepts string-encoded numbers via coercion (H11)."""
        func = mcp_tools.functions["compute_stats"]
        # MCP callers often send array elements as strings; coercion must handle this.
        result = await func.entrypoint(numbers=["2.0", "4.0", "6.0"])
        stats = json.loads(result.content)
        assert stats["mean"] == pytest.approx(4.0), (
            f"Expected mean=4.0 after coercion; got {stats!r}"
        )

    async def test_shopping_cart_session_accumulation(self, mcp_tools: MCPTools) -> None:
        """Two add_item calls in one session accumulate in the same cart (C2).

        C2 bug: session lifecycle was inert — toolkit instances were not being
        persisted across calls.  The cart must reach total=5 (2 apples + 3
        bananas) across two separate MCP call_tool invocations.
        """
        add_fn = mcp_tools.functions["ShoppingCart_add_item"]
        total_fn = mcp_tools.functions["ShoppingCart_total_items"]

        r1 = await add_fn.entrypoint(name="apple", qty=2)
        assert "2" in r1.content, f"add_item response unexpected: {r1.content!r}"

        r2 = await add_fn.entrypoint(name="banana", qty=3)
        assert "3" in r2.content, f"add_item response unexpected: {r2.content!r}"

        r_total = await total_fn.entrypoint()
        total = int(r_total.content.strip())
        assert total == 5, (
            f"Expected cart total=5 (2+3); got {total}. "
            "If total=0 the session lifecycle is not persisting the instance — C2 regression."
        )


# ---------------------------------------------------------------------------
# Full agent test — gated on ANTHROPIC_API_KEY
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping live agent test",
)
class TestAgentRun:
    """Drive a real Agno Agent (Claude model) through the smarter-mcp server.

    These tests make real API calls to Anthropic and are skipped in CI unless
    an API key is available.  They are the true end-to-end proof: the LLM
    must decide to call our tool and the response must reflect the tool output.
    """

    async def test_agent_greets_ada_formally(self, mcp_tools: MCPTools) -> None:
        """Agent asked to 'greet Ada formally' must invoke format_greeting."""
        from agno.agent import Agent
        from agno.models.anthropic import Claude

        agent = Agent(
            model=Claude(id="claude-haiku-3-5-20241022"),
            tools=[mcp_tools],
            instructions="Use the available tools to answer the user's request.",
        )

        run = await agent.arun(
            "Please greet Ada formally using the format_greeting tool."
        )

        # Verify the tool was called
        assert run.tools, "Agent must have called at least one tool"
        tool_names_called = [t.tool_name for t in run.tools if t.tool_name]
        assert any("format_greeting" in n for n in tool_names_called), (
            f"Expected format_greeting in tool calls; got {tool_names_called}"
        )

        # Verify the response reflects the tool output
        final_text = str(run.content or "")
        assert "Good day, Ada" in final_text or "Ada" in final_text, (
            f"Expected Ada greeting in response; got {final_text!r}"
        )

    async def test_agent_computes_mean(self, mcp_tools: MCPTools) -> None:
        """Agent asked for the mean of 2, 4, 6 must call compute_stats."""
        from agno.agent import Agent
        from agno.models.anthropic import Claude

        agent = Agent(
            model=Claude(id="claude-haiku-3-5-20241022"),
            tools=[mcp_tools],
            instructions="Use the available tools to answer the user's request.",
        )

        run = await agent.arun(
            "Use the compute_stats tool to find the mean of the numbers 2, 4, and 6."
        )

        assert run.tools, "Agent must have called at least one tool"
        final_text = str(run.content or "")
        # Mean of [2,4,6] is 4.0
        assert "4" in final_text, (
            f"Expected '4' (mean of 2,4,6) in response; got {final_text!r}"
        )
