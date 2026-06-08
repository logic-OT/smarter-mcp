"""Test configuration and fixtures."""

import pytest
from smarter_mcp._decorators import clear_global_registry


@pytest.fixture(autouse=True)
def reset_global_decorator_registry():
    """Clear the global @tool/@resource/@toolkit registry before each test.

    Without this, decorator-registered functions accumulate across tests in
    the same process, causing later tests to see tools from earlier ones.
    """
    clear_global_registry()
    yield
    clear_global_registry()
