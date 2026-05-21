"""Tests for the exposure filter engine."""

from __future__ import annotations

import textwrap

import pytest

from faster_mcp.extractor.filters import (
    ExposureRules,
    FilterResult,
    UnannotatedPolicy,
    VariadicPolicy,
    apply_filters,
)
from faster_mcp.extractor.surface import SurfaceExtractor
from faster_mcp.extractor.models import ExtractionResult


class TestFilters:
    """Test the exposure filter engine."""

    @pytest.fixture
    def extraction_result(self):
        source = textwrap.dedent('''
            """Module with various function types."""

            __all__ = ["public_func", "MyClass"]

            def public_func(x: int) -> int:
                """A public function."""
                return x * 2

            def _private_func(x: int) -> int:
                """A private function."""
                return x

            def not_in_all(x: int) -> int:
                """Not in __all__."""
                return x

            def variadic_func(*args, **kwargs):
                """Has variadic args."""
                pass

            def unannotated(x, y):
                """No type annotations."""
                return x + y

            class MyClass:
                def public_method(self, x: int) -> int:
                    """Public method."""
                    return x

                def _private_method(self) -> None:
                    """Private method."""
                    pass

                def __dunder_method__(self) -> None:
                    """Dunder method."""
                    pass
        ''')
        extractor = SurfaceExtractor("/tmp", use_inspect=False)
        module = extractor.extract_source(source, "test.py", "test")
        return ExtractionResult(modules=[module])

    def test_default_rules(self, extraction_result):
        """Default rules: skip private, skip dunder, warn variadic, respect __all__."""
        rules = ExposureRules()
        result = apply_filters(extraction_result, rules)

        # Should have our module
        assert len(result.modules) == 1
        module = result.modules[0]

        # public_func should be exposed
        func_names = {f.simple_name for f in module.functions}
        assert "public_func" in func_names

        # _private_func should be skipped
        assert "_private_func" not in func_names

        # not_in_all should be skipped (respects __all__)
        assert "not_in_all" not in func_names

        # variadic_func should be skipped (warn policy)
        assert "variadic_func" not in func_names

        # unannotated should be exposed (default policy is expose)
        # but it's not in __all__, so it's skipped
        assert "unannotated" not in func_names

    def test_include_private(self, extraction_result):
        rules = ExposureRules(include_private=True, respect_all=False)
        result = apply_filters(extraction_result, rules)
        module = result.modules[0]
        func_names = {f.simple_name for f in module.functions}
        assert "_private_func" in func_names

    def test_respect_all_disabled(self, extraction_result):
        rules = ExposureRules(respect_all=False)
        result = apply_filters(extraction_result, rules)
        module = result.modules[0]
        func_names = {f.simple_name for f in module.functions}
        assert "not_in_all" in func_names

    def test_variadic_expose(self, extraction_result):
        rules = ExposureRules(variadic_policy=VariadicPolicy.EXPOSE, respect_all=False)
        result = apply_filters(extraction_result, rules)
        module = result.modules[0]
        func_names = {f.simple_name for f in module.functions}
        assert "variadic_func" in func_names

    def test_explicit_exclude(self, extraction_result):
        rules = ExposureRules(
            explicit_excludes={"test.public_func"},
            respect_all=False,
        )
        result = apply_filters(extraction_result, rules)
        module = result.modules[0]
        func_names = {f.simple_name for f in module.functions}
        assert "public_func" not in func_names

    def test_explicit_include_overrides(self, extraction_result):
        """Explicit include overrides all other rules."""
        rules = ExposureRules(
            explicit_includes={"test._private_func"},
        )
        result = apply_filters(extraction_result, rules)
        module = result.modules[0]
        func_names = {f.simple_name for f in module.functions}
        assert "_private_func" in func_names

    def test_skip_reasons_logged(self, extraction_result):
        rules = ExposureRules()
        result = apply_filters(extraction_result, rules)
        skipped_names = {name for name, _ in result.skipped}
        assert any("_private" in name for name in skipped_names)

    def test_class_method_filtering(self, extraction_result):
        """Class methods should be filtered independently."""
        rules = ExposureRules(respect_all=False)
        result = apply_filters(extraction_result, rules)
        module = result.modules[0]

        assert len(module.classes) >= 1
        cls = module.classes[0]
        method_names = {m.simple_name for m in cls.methods}
        assert "public_method" in method_names
        assert "_private_method" not in method_names
