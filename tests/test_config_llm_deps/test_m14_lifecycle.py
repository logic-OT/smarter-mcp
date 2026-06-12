"""Tests for M14 — @toolkit lifecycle validation."""
from __future__ import annotations

import pytest

from smarter_mcp._decorators import clear_global_registry, toolkit


@pytest.fixture(autouse=True)
def _reset():
    clear_global_registry()
    yield
    clear_global_registry()


class TestLifecycleValidation:
    def test_valid_lifecycle_session(self):
        @toolkit(lifecycle="session")
        class MyTool:
            pass
        assert MyTool._smarter_mcp_lifecycle == "session"

    def test_valid_lifecycle_singleton(self):
        @toolkit(lifecycle="singleton")
        class MyTool:
            pass
        assert MyTool._smarter_mcp_lifecycle == "singleton"

    def test_valid_lifecycle_per_call(self):
        @toolkit(lifecycle="per-call")
        class MyTool:
            pass
        assert MyTool._smarter_mcp_lifecycle == "per-call"

    def test_invalid_lifecycle_typo_raises_value_error(self):
        """@toolkit(lifecycle='sesion') is a typo; must raise ValueError at decoration time."""
        with pytest.raises(ValueError, match="sesion"):
            @toolkit(lifecycle="sesion")
            class Bad:
                pass

    def test_invalid_lifecycle_error_lists_valid_options(self):
        """ValueError message must name the valid lifecycle options."""
        with pytest.raises(ValueError) as exc_info:
            @toolkit(lifecycle="forever")
            class Bad:
                pass
        msg = str(exc_info.value)
        assert "session" in msg
        assert "singleton" in msg
        assert "per-call" in msg

    def test_default_lifecycle_is_session(self):
        @toolkit
        class MyTool:
            pass
        assert MyTool._smarter_mcp_lifecycle == "session"

    def test_invalid_lifecycle_not_registered(self):
        """A class decorated with an invalid lifecycle must not be added to the global registry."""
        from smarter_mcp._decorators import get_global_toolkits
        try:
            @toolkit(lifecycle="bad-value")
            class ShouldNotRegister:
                pass
        except ValueError:
            pass

        toolkits = get_global_toolkits()
        names = [cls.__name__ for cls in toolkits]
        assert "ShouldNotRegister" not in names
