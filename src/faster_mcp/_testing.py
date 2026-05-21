"""
Tool Testing Framework.

Lets developers define test cases alongside their tools (via decorators or YAML)
and run them to verify tools are alive, callable, and returning expected results
before an agent ever touches them.

Usage:
    app = FasterMCP("my-server")

    @app.tool(tests=[{"params": {"name": "Alice"}, "expect": "Hello, Alice!"}])
    def greet(name: str) -> str:
        return f"Hello, {name}!"

    report = app.test()              # run all predefined tests
    report = app.test("greet")       # run tests for one tool
    report = app.test("greet", params={"name": "Bob"})  # ad-hoc test
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from faster_mcp._registry import RegisteredTool, ToolRegistry
from faster_mcp._schema import build_json_schema

logger = logging.getLogger(__name__)


# Sentinel for "no expected value specified" — distinct from None
class _UNSET_TYPE:
    """Sentinel indicating no expected value was provided."""
    def __repr__(self) -> str:
        return "<UNSET>"

    def __bool__(self) -> bool:
        return False

UNSET = _UNSET_TYPE()


# ──────────────────────────────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────────────────────────────

@dataclass
class TestCase:
    """A single test case for a tool.

    Attributes:
        params: Input keyword arguments to pass to the tool.
        expect: Expected return value (exact equality check). UNSET = skip.
        expect_type: Expected return type name (e.g., "list", "dict"). None = skip.
    """
    params: dict[str, Any]
    expect: Any = UNSET
    expect_type: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TestCase:
        """Create a TestCase from the dict format used in decorators and YAML.

        Expected dict shape:
            {"params": {...}, "expect": ..., "expect_type": "list"}
        """
        return cls(
            params=data.get("params", {}),
            expect=data.get("expect", UNSET),
            expect_type=data.get("expect_type"),
        )


@dataclass
class TestResult:
    """Result of running a single test case against a tool.

    Attributes:
        tool_name: Name of the tool that was tested.
        namespace: Namespace the tool belongs to.
        passed: Whether all checks passed.
        output: Actual return value from the tool (None if execution failed).
        error: Error message if any check failed.
        latency_ms: Execution time in milliseconds.
        check_results: Per-check pass/fail breakdown.
    """
    tool_name: str
    namespace: str
    passed: bool
    output: Any = None
    error: str | None = None
    latency_ms: float = 0.0
    check_results: dict[str, bool] = field(default_factory=dict)


@dataclass
class TestReport:
    """Aggregate result from running tests across multiple tools.

    Attributes:
        results: Individual test results.
        total: Total number of test cases run.
        passed: Number of test cases that passed.
        failed: Number of test cases that failed.
        skipped: Number of tools that had no test cases defined.
    """
    results: list[TestResult]
    total: int
    passed: int
    failed: int
    skipped: int

    def summary(self) -> str:
        """Human-readable one-line summary."""
        return f"Results: {self.passed} passed, {self.failed} failed, {self.skipped} skipped"


# ──────────────────────────────────────────────────────────────────────
# Test runner engine
# ──────────────────────────────────────────────────────────────────────

class ToolTestRunner:
    """Executes test cases against registered tools.

    The runner calls the raw tool.fn directly (not the FastMCP wrapper).
    For class methods, it creates a test instance via the InstanceManager
    with no MCP Context (falls back to per-call). For async functions,
    it runs them in the current event loop or creates one.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        instance_manager: Any = None,
    ):
        """
        Args:
            registry: The tool registry to pull tools from.
            instance_manager: Optional InstanceManager for class-based tools.
        """
        self._registry = registry
        self._instance_manager = instance_manager

    def run_all(self) -> TestReport:
        """Run all predefined test cases for all registered tools."""
        results: list[TestResult] = []
        skipped = 0

        for tool in self._registry.get_all_tools():
            if not tool.tests:
                skipped += 1
                continue

            for test_dict in tool.tests:
                case = TestCase.from_dict(test_dict)
                result = self._run_single(tool, case)
                results.append(result)

        passed = sum(1 for r in results if r.passed)
        failed = sum(1 for r in results if not r.passed)

        return TestReport(
            results=results,
            total=len(results),
            passed=passed,
            failed=failed,
            skipped=skipped,
        )

    def run_tool(self, tool_name: str) -> TestReport:
        """Run all predefined test cases for a specific tool.

        Args:
            tool_name: Name of the tool to test.

        Raises:
            ValueError: If no tool with that name exists.
        """
        tool = self._find_tool(tool_name)
        results: list[TestResult] = []
        skipped = 0

        if not tool.tests:
            skipped = 1
        else:
            for test_dict in tool.tests:
                case = TestCase.from_dict(test_dict)
                result = self._run_single(tool, case)
                results.append(result)

        passed = sum(1 for r in results if r.passed)
        failed = sum(1 for r in results if not r.passed)

        return TestReport(
            results=results,
            total=len(results),
            passed=passed,
            failed=failed,
            skipped=skipped,
        )

    def run_adhoc(self, tool_name: str, params: dict[str, Any]) -> TestReport:
        """Run an ad-hoc test with custom params (no expected value).

        Args:
            tool_name: Name of the tool to test.
            params: Input kwargs to pass to the tool.
        """
        tool = self._find_tool(tool_name)
        case = TestCase(params=params)
        result = self._run_single(tool, case)

        return TestReport(
            results=[result],
            total=1,
            passed=1 if result.passed else 0,
            failed=0 if result.passed else 1,
            skipped=0,
        )

    # ──────────────────────────────────────────────────────────────────
    # Internal: execute a single test case
    # ──────────────────────────────────────────────────────────────────

    def _run_single(self, tool: RegisteredTool, case: TestCase) -> TestResult:
        """Run a single TestCase against a single RegisteredTool."""
        checks: dict[str, bool] = {}
        error: str | None = None
        output: Any = None
        latency_ms: float = 0.0

        # ── Check 1: callable ──
        checks["callable"] = callable(tool.fn)
        if not checks["callable"]:
            return TestResult(
                tool_name=tool.name,
                namespace=tool.namespace,
                passed=False,
                error="Tool function is not callable",
                check_results=checks,
            )

        # ── Check 2: schema ──
        try:
            schema = build_json_schema(tool)
            # A valid schema must be a dict with "type" and "properties"
            checks["schema"] = (
                isinstance(schema, dict)
                and "type" in schema
                and "properties" in schema
            )
        except Exception as e:
            checks["schema"] = False
            error = f"Schema build failed: {e}"

        # ── Check 3: instance (class methods only) ──
        instance = None
        if tool.class_name:
            try:
                instance = self._resolve_test_instance(tool)
                checks["instance"] = instance is not None
            except Exception as e:
                checks["instance"] = False
                error = f"Instance creation failed: {e}"
                return TestResult(
                    tool_name=tool.name,
                    namespace=tool.namespace,
                    passed=False,
                    error=error,
                    check_results=checks,
                )

        # ── Check 4: execution ──
        try:
            start = time.perf_counter()
            output = self._execute(tool, case.params, instance)
            latency_ms = (time.perf_counter() - start) * 1000
            checks["execution"] = True
        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000
            checks["execution"] = False
            error = f"{type(e).__name__}: {e}"
            return TestResult(
                tool_name=tool.name,
                namespace=tool.namespace,
                passed=False,
                output=None,
                error=error,
                latency_ms=latency_ms,
                check_results=checks,
            )

        # ── Check 5: return_type ──
        if case.expect_type is not None:
            actual_type = type(output).__name__
            checks["return_type"] = actual_type == case.expect_type
            if not checks["return_type"]:
                error = f"Expected type '{case.expect_type}', got '{actual_type}'"

        # ── Check 6: return_value ──
        if not isinstance(case.expect, _UNSET_TYPE):
            checks["return_value"] = output == case.expect
            if not checks["return_value"]:
                error = f"Expected {case.expect!r}, got {output!r}"

        # ── Check 7: serializable ──
        try:
            json.dumps(output, default=str)
            checks["serializable"] = True
        except (TypeError, ValueError) as e:
            checks["serializable"] = False
            if error is None:
                error = f"Return value not JSON-serializable: {e}"

        passed = all(checks.values())

        return TestResult(
            tool_name=tool.name,
            namespace=tool.namespace,
            passed=passed,
            output=output,
            error=error if not passed else None,
            latency_ms=latency_ms,
            check_results=checks,
        )

    def _execute(self, tool: RegisteredTool, params: dict[str, Any], instance: Any = None) -> Any:
        """Call the tool's function with the given params.

        Handles sync vs async, and injects `self` for class methods.
        """
        fn = tool.fn

        if instance is not None:
            # Bind the method to the instance
            if tool.is_async:
                result = self._run_async(fn, instance, **params)
            else:
                result = fn(instance, **params)
        else:
            if tool.is_async:
                result = self._run_async(fn, **params)
            else:
                result = fn(**params)

        return result

    def _run_async(self, fn, *args, **kwargs) -> Any:
        """Run an async function, creating an event loop if necessary."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # We're already inside an event loop (e.g., pytest-asyncio).
            # Create a new coroutine and use loop.run_until_complete
            # This won't work inside a running loop, so we use a thread.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, fn(*args, **kwargs))
                return future.result()
        else:
            return asyncio.run(fn(*args, **kwargs))

    def _resolve_test_instance(self, tool: RegisteredTool) -> Any:
        """Create a test instance for a class-based tool.

        Uses the InstanceManager if available, otherwise tries cls() directly.
        """
        if self._instance_manager:
            # Look up the toolkit in the registry to get the class object
            toolkit = self._registry._toolkits.get(tool.class_name)
            if toolkit:
                return self._instance_manager.get_instance(
                    tool.class_name, toolkit.cls, ctx=None
                )

        # Fallback: try to resolve the class from the tool's module
        if tool.extracted_obj and tool.extracted_obj.class_name:
            import importlib
            parts = tool.extracted_obj.qualified_name.rsplit(".", 2)
            if len(parts) >= 2:
                module_name = parts[0]
                class_name = parts[1] if len(parts) == 3 else tool.extracted_obj.class_name
                try:
                    mod = importlib.import_module(module_name)
                    cls_obj = getattr(mod, class_name)
                    return cls_obj()
                except Exception:
                    pass

        raise RuntimeError(
            f"Cannot create test instance for class '{tool.class_name}'. "
            f"Register it as a toolkit or provide an InstanceConfig."
        )

    def _find_tool(self, tool_name: str) -> RegisteredTool:
        """Look up a tool by name across all namespaces."""
        for tool in self._registry.get_all_tools():
            if tool.name == tool_name:
                return tool
        raise ValueError(f"No tool named '{tool_name}' found in the registry")
