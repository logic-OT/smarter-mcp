"""E2E test server for Agno + smarter-mcp integration tests.

Exposes three tool surfaces that exercise headline framework features:
  - format_greeting:   plain string return (proves C1 — returned strings are
                       served as text, not re-encoded as broken images).
  - compute_stats:     typed list[float] param + dict return (proves H11 —
                       schema generation and coercion round-trip correctly).
  - ShoppingCart:      session-lifecycle toolkit (proves C2 — toolkit instance
                       persists and accumulates state across calls in one MCP
                       session).

The ``app`` name is required: smarter_mcp.cli._detect.detect_app() scans the
module for a SmarterMCP instance named "app", "server", "mcp", or "smarter_mcp".
"""

from __future__ import annotations

import statistics

from smarter_mcp import SmarterMCP
from smarter_mcp._decorators import tool, toolkit


@tool
def format_greeting(name: str, formal: bool = False) -> str:
    """Format a greeting message for a person.

    Returns a formal or informal greeting string.
    """
    if formal:
        return f"Good day, {name}."
    return f"Hello, {name}!"


@tool
def compute_stats(numbers: list[float]) -> dict:
    """Compute basic statistics (mean, min, max) for a list of numbers.

    Exercises schema coercion: the MCP caller passes a JSON array and the
    framework must coerce each element to float before calling this function.
    """
    if not numbers:
        return {"mean": 0.0, "min": 0.0, "max": 0.0}
    return {
        "mean": statistics.mean(numbers),
        "min": float(min(numbers)),
        "max": float(max(numbers)),
    }


@toolkit(lifecycle="session")
class ShoppingCart:
    """In-memory shopping cart that persists across tool calls within a session.

    The session lifecycle means one ShoppingCart instance is created per MCP
    session (i.e. per stdio connection) and reused for every subsequent call in
    that session.  Two add_item calls therefore accumulate in the same cart.
    """

    def __init__(self) -> None:
        self._items: dict[str, int] = {}

    @tool
    def add_item(self, name: str, qty: int) -> str:
        """Add or increment an item in the shopping cart."""
        self._items[name] = self._items.get(name, 0) + qty
        return f"Added {qty} x {name}. Cart now has {sum(self._items.values())} items."

    @tool
    def total_items(self) -> int:
        """Return the total quantity of all items across all cart entries."""
        return sum(self._items.values())


# Required: detect_app() searches for a SmarterMCP instance at module level.
app = SmarterMCP("e2e-test-server")
