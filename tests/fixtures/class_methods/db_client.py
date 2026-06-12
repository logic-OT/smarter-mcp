"""Fixture: class-based module with methods, properties, inheritance."""
from __future__ import annotations


class BaseClient:
    """Base client with common functionality."""

    def connect(self) -> bool:
        """Establish connection."""
        return True

    def disconnect(self) -> None:
        """Close connection."""
        pass


class DatabaseClient(BaseClient):
    """A database client with various method types.

    :param host: Database host address.
    :param port: Database port number.
    """

    def __init__(self, host: str = "localhost", port: int = 5432):
        self.host = host
        self.port = port
        self._connection = None

    @property
    def is_connected(self) -> bool:
        """Whether the client is currently connected."""
        return self._connection is not None

    @property
    def connection_string(self) -> str:
        """The full connection string."""
        return f"{self.host}:{self.port}"

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        """Execute a SQL query.

        Args:
            sql: The SQL query string.
            params: Optional query parameters.

        Returns:
            List of result rows as dictionaries.
        """
        return [{"sql": sql}]

    async def query_async(self, sql: str) -> list[dict]:
        """Execute a SQL query asynchronously.

        Args:
            sql: The SQL query string.

        Returns:
            List of result rows.
        """
        return [{"sql": sql}]

    @classmethod
    def from_url(cls, url: str) -> DatabaseClient:
        """Create a client from a connection URL.

        Args:
            url: Database connection URL.

        Returns:
            A new DatabaseClient instance.
        """
        return cls(host=url)

    @staticmethod
    def parse_url(url: str) -> dict:
        """Parse a database URL into components.

        Args:
            url: The URL to parse.

        Returns:
            Dictionary of URL components.
        """
        return {"url": url}

    def _internal_method(self) -> None:
        """Private method — should be excluded by default."""
        pass
