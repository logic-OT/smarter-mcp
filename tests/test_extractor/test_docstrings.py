"""Tests for the docstring parser."""

from __future__ import annotations

from smarter_mcp.extractor.docstrings import detect_format, parse_docstring
from smarter_mcp.extractor.models import DocstringFormat


class TestFormatDetection:
    def test_google_style(self):
        doc = """Do something.

        Args:
            x: The value.
        """
        assert detect_format(doc) == DocstringFormat.GOOGLE

    def test_numpy_style(self):
        doc = """Do something.

        Parameters
        ----------
        x : int
            The value.
        """
        assert detect_format(doc) == DocstringFormat.NUMPY

    def test_sphinx_style(self):
        doc = """Do something.

        :param x: The value.
        :type x: int
        """
        assert detect_format(doc) == DocstringFormat.SPHINX

    def test_plain(self):
        doc = "Just a simple description."
        assert detect_format(doc) == DocstringFormat.PLAIN


class TestGoogleDocstring:
    def test_basic_parsing(self):
        doc = """Add two numbers.

        Args:
            a: The first number.
            b: The second number.

        Returns:
            The sum of a and b.
        """
        result = parse_docstring(doc)
        assert result.format == DocstringFormat.GOOGLE
        assert "Add two numbers" in result.summary
        assert result.params["a"] == "The first number."
        assert result.params["b"] == "The second number."
        assert "sum" in result.returns.lower()

    def test_typed_params(self):
        doc = """Do something.

        Args:
            x (int): An integer.
            y (str): A string.
        """
        result = parse_docstring(doc)
        assert result.param_types["x"] == "int"
        assert result.param_types["y"] == "str"
        assert result.params["x"] == "An integer."

    def test_raises(self):
        doc = """Do something risky.

        Raises:
            ValueError: If the value is invalid.
            TypeError: If the type is wrong.
        """
        result = parse_docstring(doc)
        assert "ValueError" in result.raises
        assert "TypeError" in result.raises


class TestNumpyDocstring:
    def test_basic_parsing(self):
        doc = """Compute the mean.

        Parameters
        ----------
        data : list
            The data to average.
        weights : list, optional
            Optional weights.

        Returns
        -------
        float
            The weighted mean.
        """
        result = parse_docstring(doc)
        assert result.format == DocstringFormat.NUMPY
        assert "Compute the mean" in result.summary
        assert "data" in result.params
        assert result.param_types.get("data") == "list"
        assert result.returns_type == "float"


class TestSphinxDocstring:
    def test_basic_parsing(self):
        doc = """Connect to database.

        :param host: The hostname.
        :type host: str
        :param port: The port number.
        :type port: int
        :returns: Connection status.
        :rtype: bool
        """
        result = parse_docstring(doc)
        assert result.format == DocstringFormat.SPHINX
        assert result.params["host"] == "The hostname."
        assert result.param_types["host"] == "str"
        assert result.params["port"] == "The port number."
        assert result.returns_type == "bool"

    def test_raises(self):
        doc = """Do something.

        :raises ValueError: Bad value.
        """
        result = parse_docstring(doc)
        assert "ValueError" in result.raises


class TestEmptyDocstring:
    def test_none(self):
        result = parse_docstring(None)
        assert result.summary == ""

    def test_empty_string(self):
        result = parse_docstring("")
        assert result.summary == ""

    def test_whitespace(self):
        result = parse_docstring("   ")
        assert result.summary == ""
