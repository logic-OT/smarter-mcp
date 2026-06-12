"""
Tests for the Tool Testing Framework.

Verifies that app.test() correctly runs predefined and ad-hoc test cases,
reports pass/fail/skip, and validates schemas, return types, return values,
and serializability.
"""

import pytest

from smarter_mcp import SmarterMCP, tool

# ──────────────────────────────────────────────────────────────────────
# Helpers: set up a SmarterMCP app with various tool shapes
# ──────────────────────────────────────────────────────────────────────

def _build_app() -> SmarterMCP:
    """Create a SmarterMCP app with a mix of tools for testing."""
    app = SmarterMCP(name="test-server")

    # 1. Simple passing tool with exact-match test case
    @tool(
        description="Greet a user by name",
        tests=[
            {"params": {"name": "Alice"}, "expect": "Hello, Alice!"},
            {"params": {"name": "Bob"}, "expect": "Hello, Bob!"},
        ],
    )
    def greet(name: str) -> str:
        return f"Hello, {name}!"

    # 2. Tool with expect_type check
    @tool(
        description="Add two numbers",
        tests=[
            {"params": {"a": 2, "b": 3}, "expect": 5, "expect_type": "int"},
        ],
    )
    def add(a: int, b: int) -> int:
        return a + b

    # 3. Tool that deliberately raises an exception
    @tool(
        description="Always fails",
        tests=[
            {"params": {"x": 1}},
        ],
    )
    def broken(x: int) -> int:
        raise ValueError("Intentional failure")

    # 4. Tool with no test cases defined (should be skipped)
    @tool(description="No tests defined")
    def no_tests(value: str) -> str:
        return value

    # 5. Tool whose return doesn't match expect
    @tool(
        description="Returns wrong value",
        tests=[
            {"params": {"x": 1}, "expect": 999},
        ],
    )
    def wrong_value(x: int) -> int:
        return x + 1

    # 6. Tool with wrong return type
    @tool(
        description="Returns a string, not an int",
        tests=[
            {"params": {"x": 1}, "expect_type": "int"},
        ],
    )
    def wrong_type(x: int) -> str:
        return str(x)

    # 7. Async tool
    @tool(
        description="Async greeting",
        tests=[
            {"params": {"name": "Async"}, "expect": "Hi, Async!"},
        ],
    )
    async def async_greet(name: str) -> str:
        return f"Hi, {name}!"

    # 8. Tool that returns a non-serializable object
    @tool(
        description="Returns a set (not JSON-serializable natively)",
        tests=[
            {"params": {}},
        ],
    )
    def returns_set() -> set:
        return {1, 2, 3}

    return app


# ──────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────

class TestToolTestingFramework:
    """Tests for the SmarterMCP.test() method and ToolTestRunner."""

    def setup_method(self):
        """Create a fresh app for each test."""
        self.app = _build_app()

    # ── Passing tool ──

    def test_passing_tool(self):
        """A tool with correct expect values should pass."""
        report = self.app.test("greet")
        assert report.passed == 2
        assert report.failed == 0
        for result in report.results:
            assert result.passed is True
            assert result.check_results["callable"] is True
            assert result.check_results["schema"] is True
            assert result.check_results["execution"] is True
            assert result.check_results["return_value"] is True

    # ── Failing expect value ──

    def test_failing_expect_value(self):
        """A tool whose output doesn't match expect should fail."""
        report = self.app.test("wrong_value")
        assert report.failed == 1
        result = report.results[0]
        assert result.passed is False
        assert result.check_results["return_value"] is False
        assert "Expected 999" in result.error

    # ── Failing execution ──

    def test_failing_execution(self):
        """A tool that raises an exception should fail the execution check."""
        report = self.app.test("broken")
        assert report.failed == 1
        result = report.results[0]
        assert result.passed is False
        assert result.check_results["execution"] is False
        assert "Intentional failure" in result.error

    # ── Return type check ──

    def test_expect_type_passes(self):
        """expect_type='int' passes when the tool returns an int."""
        report = self.app.test("add")
        assert report.passed == 1
        result = report.results[0]
        assert result.check_results["return_type"] is True
        assert result.output == 5

    def test_wrong_type(self):
        """expect_type='int' fails when the tool returns a string."""
        report = self.app.test("wrong_type")
        assert report.failed == 1
        result = report.results[0]
        assert result.passed is False
        assert result.check_results["return_type"] is False
        assert "Expected type 'int'" in result.error

    # ── No test cases ──

    def test_no_test_cases_skipped(self):
        """Tools with no predefined test cases should be skipped."""
        report = self.app.test("no_tests")
        assert report.skipped == 1
        assert report.total == 0

    # ── Test all tools ──

    def test_all_tools(self):
        """app.test() runs tests across all registered tools."""
        report = self.app.test()
        # We have 8 tools: greet(2), add(1), broken(1), no_tests(0),
        # wrong_value(1), wrong_type(1), async_greet(1), returns_set(1)
        assert report.total == 8  # total test cases
        assert report.skipped == 1  # no_tests
        # greet(2 pass) + add(1 pass) + async_greet(1 pass) = 4 pass
        # broken(1 fail) + wrong_value(1 fail) + wrong_type(1 fail) = 3 fail
        # returns_set(1) — passes because json.dumps with default=str works
        assert report.passed >= 4
        assert report.failed >= 2

    # ── Single tool ──

    def test_single_tool(self):
        """app.test('add') runs only that tool's tests."""
        report = self.app.test("add")
        assert report.total == 1
        assert report.passed == 1

    # ── Ad-hoc params ──

    def test_adhoc_params(self):
        """app.test('greet', params={...}) creates and runs an ad-hoc test."""
        report = self.app.test("greet", params={"name": "Charlie"})
        assert report.total == 1
        assert report.passed == 1
        assert report.results[0].output == "Hello, Charlie!"

    # ── Async tool ──

    def test_async_tool(self):
        """An async tool is tested correctly."""
        report = self.app.test("async_greet")
        assert report.passed == 1
        assert report.results[0].output == "Hi, Async!"

    # ── Serializable check ──

    def test_serializable_with_default_str(self):
        """json.dumps(result, default=str) is used, so sets become serializable."""
        report = self.app.test("returns_set")
        result = report.results[0]
        # With default=str, sets serialize fine — this should pass
        assert result.check_results["serializable"] is True

    # ── Unknown tool ──

    def test_unknown_tool_raises(self):
        """Testing a non-existent tool raises ValueError."""
        with pytest.raises(ValueError, match="No tool named"):
            self.app.test("nonexistent_tool")

    # ── Latency tracking ──

    def test_latency_tracked(self):
        """Each test result has a latency_ms >= 0."""
        report = self.app.test("greet")
        for result in report.results:
            assert result.latency_ms >= 0

    # ── Report summary ──

    def test_report_summary(self):
        """TestReport.summary() returns a human-readable string."""
        report = self.app.test()
        summary = report.summary()
        assert "passed" in summary
        assert "failed" in summary
        assert "skipped" in summary
