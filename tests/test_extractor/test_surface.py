"""Tests for the surface extraction engine."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from faster_mcp.extractor.models import CallableKind, ParamKind
from faster_mcp.extractor.surface import SurfaceExtractor

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


class TestSimpleFunctions:
    """Test extraction of module-level functions."""

    @pytest.fixture
    def module(self):
        extractor = SurfaceExtractor(
            FIXTURES_DIR / "simple_functions",
            use_inspect=False,
        )
        result = extractor.extract()
        assert len(result.modules) >= 1
        # Find math_utils module
        for m in result.modules:
            if "math_utils" in m.module_name:
                return m
        pytest.fail("math_utils module not found in extraction results")

    def test_function_count(self, module):
        """All public + private functions are extracted (filtering is separate)."""
        names = {f.simple_name for f in module.functions}
        assert "add" in names
        assert "multiply" in names
        assert "fetch_data" in names
        assert "greet" in names
        assert "_private_helper" in names
        assert "process_items" in names

    def test_annotated_params(self, module):
        add_func = next(f for f in module.functions if f.simple_name == "add")
        assert add_func.kind == CallableKind.FUNCTION
        assert not add_func.is_async
        assert add_func.return_type == "float"

        params = add_func.non_self_params
        assert len(params) == 2
        assert params[0].name == "a"
        assert params[0].annotation == "float"
        assert params[1].name == "b"
        assert params[1].annotation == "float"

    def test_async_function(self, module):
        fetch = next(f for f in module.functions if f.simple_name == "fetch_data")
        assert fetch.is_async
        assert fetch.return_type == "dict"
        params = fetch.non_self_params
        assert len(params) == 2
        assert params[1].name == "timeout"
        assert params[1].has_default
        assert params[1].default == 30

    def test_unannotated_function(self, module):
        greet = next(f for f in module.functions if f.simple_name == "greet")
        assert greet.kind == CallableKind.FUNCTION
        params = greet.non_self_params
        assert len(params) == 2
        assert params[0].name == "name"
        # NumPy docstring parser enriches annotation from "name : str" syntax
        assert params[0].annotation == "str"
        assert params[1].name == "greeting"
        assert params[1].default == "Hello"

    def test_variadic_detection(self, module):
        process = next(f for f in module.functions if f.simple_name == "process_items")
        assert process.has_variadic
        variadic_params = [p for p in process.parameters if p.is_variadic]
        assert len(variadic_params) == 2  # *args and **kwargs

    def test_all_exports(self, module):
        assert module.all_exports is not None
        assert set(module.all_exports) == {"add", "multiply", "fetch_data", "greet"}

    def test_docstring_param_descriptions(self, module):
        """Docstring parsing should extract per-parameter descriptions."""
        add_func = next(f for f in module.functions if f.simple_name == "add")
        params = add_func.non_self_params
        assert params[0].description is not None
        assert "first" in params[0].description.lower()

    def test_numpy_docstring_parsing(self, module):
        """NumPy-style docstrings should be parsed correctly."""
        greet = next(f for f in module.functions if f.simple_name == "greet")
        params = greet.non_self_params
        # NumPy style should extract param descriptions
        assert params[0].description is not None


class TestClassMethods:
    """Test extraction of classes and their methods."""

    @pytest.fixture
    def module(self):
        extractor = SurfaceExtractor(
            FIXTURES_DIR / "class_methods",
            use_inspect=False,
        )
        result = extractor.extract()
        assert len(result.modules) >= 1
        for m in result.modules:
            if "db_client" in m.module_name:
                return m
        pytest.fail("db_client module not found")

    def test_class_extraction(self, module):
        assert len(module.classes) == 2  # BaseClient + DatabaseClient

    def test_database_client_methods(self, module):
        db_cls = next(c for c in module.classes if c.name == "DatabaseClient")
        method_names = {m.simple_name for m in db_cls.methods}

        assert "query" in method_names
        assert "query_async" in method_names
        assert "from_url" in method_names
        assert "parse_url" in method_names
        assert "_internal_method" in method_names
        # __init__ should NOT be in methods (it's captured in init_params)
        assert "__init__" not in method_names

    def test_method_kinds(self, module):
        db_cls = next(c for c in module.classes if c.name == "DatabaseClient")

        query = next(m for m in db_cls.methods if m.simple_name == "query")
        assert query.kind == CallableKind.METHOD
        assert not query.is_async

        query_async = next(m for m in db_cls.methods if m.simple_name == "query_async")
        assert query_async.kind == CallableKind.METHOD
        assert query_async.is_async

        from_url = next(m for m in db_cls.methods if m.simple_name == "from_url")
        assert from_url.kind == CallableKind.CLASSMETHOD

        parse_url = next(m for m in db_cls.methods if m.simple_name == "parse_url")
        assert parse_url.kind == CallableKind.STATICMETHOD

    def test_properties(self, module):
        db_cls = next(c for c in module.classes if c.name == "DatabaseClient")
        prop_names = {p.simple_name for p in db_cls.properties}
        assert "is_connected" in prop_names
        assert "connection_string" in prop_names

    def test_init_params(self, module):
        db_cls = next(c for c in module.classes if c.name == "DatabaseClient")
        assert len(db_cls.init_params) >= 2
        host_param = next(p for p in db_cls.init_params if p.name == "host")
        assert host_param.default == "localhost"
        port_param = next(p for p in db_cls.init_params if p.name == "port")
        assert port_param.default == 5432

    def test_tool_names(self, module):
        db_cls = next(c for c in module.classes if c.name == "DatabaseClient")
        query = next(m for m in db_cls.methods if m.simple_name == "query")
        assert query.tool_name == "DatabaseClient_query"

    def test_non_self_params(self, module):
        db_cls = next(c for c in module.classes if c.name == "DatabaseClient")
        query = next(m for m in db_cls.methods if m.simple_name == "query")
        params = query.non_self_params
        # Should exclude 'self'
        param_names = [p.name for p in params]
        assert "self" not in param_names
        assert "sql" in param_names

    def test_bases(self, module):
        db_cls = next(c for c in module.classes if c.name == "DatabaseClient")
        assert "BaseClient" in db_cls.bases


class TestSourceStringExtraction:
    """Test extracting from source strings directly."""

    def test_simple_extraction(self):
        source = textwrap.dedent('''
            def hello(name: str) -> str:
                """Say hello."""
                return f"Hello, {name}!"
        ''')
        extractor = SurfaceExtractor("/tmp", use_inspect=False)
        module = extractor.extract_source(source, "test.py", "test")
        assert len(module.functions) == 1
        func = module.functions[0]
        assert func.simple_name == "hello"
        assert func.return_type == "str"

    def test_type_inference_from_defaults(self):
        source = textwrap.dedent('''
            def greet(name, count=3, verbose=True):
                """Greet someone multiple times."""
                pass
        ''')
        extractor = SurfaceExtractor("/tmp", use_inspect=False)
        module = extractor.extract_source(source, "test.py", "test")
        func = module.functions[0]
        params = func.non_self_params

        count = next(p for p in params if p.name == "count")
        assert count.inferred_type == "int"

        verbose = next(p for p in params if p.name == "verbose")
        assert verbose.inferred_type == "bool"
