"""Tests for HealthEndpoint.get_health().

Exercises:
- Simplified status expression (degraded iff failed_modules > 0)
- Both extraction_errors and import_failures present as distinct integer fields
- failed_modules = extraction_errors + import_failures
- Healthy path returns all zeros and status 'healthy'
"""

from __future__ import annotations

from unittest.mock import MagicMock

from smarter_mcp._registry import ToolRegistry
from smarter_mcp.extractor.models import ExtractionResult
from smarter_mcp.server.health import HealthEndpoint
from smarter_mcp.server.router import NamespaceRouter


def _make_health(
    extraction_errors: int = 0,
    import_failures: int = 0,
    authenticated: bool = True,
) -> dict:
    """Build a minimal HealthEndpoint and return its get_health() dict.

    ``authenticated=True`` by default so existing detail-field assertions work.
    Tests for the H8 auth-gating behaviour are in test_security.py.
    """
    router = MagicMock(spec=NamespaceRouter)
    router.namespaces = []
    registry = ToolRegistry()

    extraction = ExtractionResult(
        errors=[f"err{i}" for i in range(extraction_errors)],
    )
    ep = HealthEndpoint(
        router=router,
        registry=registry,
        extraction_result=extraction,
        import_failure_count=import_failures,
    )
    return ep.get_health(authenticated=authenticated)


class TestHealthFields:
    def test_healthy_when_no_failures(self):
        health = _make_health(extraction_errors=0, import_failures=0)
        assert health["status"] == "healthy"
        assert health["extraction_errors"] == 0
        assert health["import_failures"] == 0
        assert health["failed_modules"] == 0

    def test_degraded_on_extraction_errors(self):
        health = _make_health(extraction_errors=2, import_failures=0)
        assert health["status"] == "degraded"
        assert health["extraction_errors"] == 2
        assert health["import_failures"] == 0
        assert health["failed_modules"] == 2

    def test_degraded_on_import_failures(self):
        health = _make_health(extraction_errors=0, import_failures=3)
        assert health["status"] == "degraded"
        assert health["extraction_errors"] == 0
        assert health["import_failures"] == 3
        assert health["failed_modules"] == 3

    def test_degraded_on_both_failure_kinds(self):
        health = _make_health(extraction_errors=1, import_failures=2)
        assert health["status"] == "degraded"
        assert health["extraction_errors"] == 1
        assert health["import_failures"] == 2
        assert health["failed_modules"] == 3

    def test_extraction_errors_and_import_failures_are_separate_fields(self):
        """Both fields must be individually present — not merged into one count."""
        health = _make_health(extraction_errors=2, import_failures=5)
        assert "extraction_errors" in health, (
            "extraction_errors must be a distinct field in the health JSON"
        )
        assert "import_failures" in health, (
            "import_failures must be a distinct field in the health JSON"
        )
        assert health["extraction_errors"] != health["import_failures"], (
            "The two counts must be individually surfaced, not collapsed"
        )

    def test_failed_modules_is_sum(self):
        health = _make_health(extraction_errors=4, import_failures=6)
        assert health["failed_modules"] == health["extraction_errors"] + health["import_failures"]

    def test_no_extraction_result(self):
        """HealthEndpoint with no extraction result must still return valid JSON."""
        router = MagicMock(spec=NamespaceRouter)
        router.namespaces = []
        registry = ToolRegistry()
        ep = HealthEndpoint(router=router, registry=registry)
        health = ep.get_health(authenticated=True)
        assert health["status"] == "healthy"
        assert health["extraction_errors"] == 0
        assert health["import_failures"] == 0
