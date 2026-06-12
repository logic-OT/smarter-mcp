"""Fixture: simple module-level functions with various annotation styles."""


def add(a: float, b: float) -> float:
    """Add two numbers together.

    Args:
        a: The first number.
        b: The second number.

    Returns:
        The sum of a and b.
    """
    return a + b


def multiply(a: float, b: float) -> float:
    """Multiply two numbers."""
    return a * b


async def fetch_data(url: str, timeout: int = 30) -> dict:
    """Fetch data from a URL.

    Args:
        url: The URL to fetch from.
        timeout: Request timeout in seconds.

    Returns:
        dict: The response data.
    """
    return {"url": url, "timeout": timeout}


def greet(name, greeting="Hello"):
    """Greet someone (no type annotations).

    Parameters
    ----------
    name : str
        The person's name.
    greeting : str
        The greeting to use.

    Returns
    -------
    str
        The greeting message.
    """
    return f"{greeting}, {name}!"


def _private_helper():
    """This should be excluded by default."""
    pass


def process_items(*args, **kwargs):
    """This has variadic args — should be skipped with warning."""
    return list(args)


__all__ = ["add", "fetch_data", "greet", "multiply"]
