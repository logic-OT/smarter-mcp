"""Tests for H5 — JSON cache replacing pickle.

- Round-trip an ExtractedModule through the JSON cache.
- Corrupt file → miss (not exception propagated to caller).
- No pickle import remains in cache.py.
- Cache anchored to source_root.
"""

from __future__ import annotations

from smarter_mcp.extractor.cache import ExtractionCache, _decode_module, _encode_module
from smarter_mcp.extractor.models import (
    MISSING,
    NON_LITERAL,
    CallableKind,
    ExtractedCallable,
    ExtractedClass,
    ExtractedModule,
    ExtractedParam,
    ParamKind,
)


def _make_sample_module() -> ExtractedModule:
    """Build a representative ExtractedModule for round-trip tests."""
    param_with_default = ExtractedParam(
        name="x",
        annotation="int",
        default=42,
        kind=ParamKind.POSITIONAL_OR_KEYWORD,
    )
    param_missing = ExtractedParam(
        name="y",
        annotation="str",
        default=MISSING,
        kind=ParamKind.KEYWORD_ONLY,
        description="a string parameter",
    )
    param_non_literal = ExtractedParam(
        name="ts",
        annotation="datetime",
        default=NON_LITERAL,
        kind=ParamKind.POSITIONAL_OR_KEYWORD,
    )
    fn = ExtractedCallable(
        qualified_name="mymod.my_func",
        kind=CallableKind.FUNCTION,
        module_path="mymod.py",
        parameters=[param_with_default, param_missing, param_non_literal],
        return_type="str",
        docstring="Does a thing.",
        is_async=True,
        has_variadic=False,
        decorators=["@something"],
        source_lines=(10, 20),
    )
    method = ExtractedCallable(
        qualified_name="mymod.MyClass.do_work",
        kind=CallableKind.METHOD,
        module_path="mymod.py",
        class_name="MyClass",
        parameters=[param_with_default],
    )
    cls = ExtractedClass(
        name="MyClass",
        qualified_name="mymod.MyClass",
        module_path="mymod.py",
        docstring="A class.",
        bases=["Base"],
        methods=[method],
        properties=[],
        init_params=[param_missing],
        decorators=[],
        source_lines=(5, 50),
    )
    return ExtractedModule(
        module_path="mymod.py",
        module_name="mymod",
        functions=[fn],
        classes=[cls],
        docstring="Module docstring.",
        all_exports=["my_func", "MyClass"],
    )


class TestJsonCacheRoundTrip:
    def test_encode_decode_module(self):
        """A module round-tripped through encode/decode must be structurally identical."""
        original = _make_sample_module()
        encoded = _encode_module(original)
        recovered = _decode_module(encoded)

        assert recovered.module_name == original.module_name
        assert recovered.module_path == original.module_path
        assert recovered.docstring == original.docstring
        assert recovered.all_exports == original.all_exports

        # Functions
        assert len(recovered.functions) == 1
        fn = recovered.functions[0]
        orig_fn = original.functions[0]
        assert fn.qualified_name == orig_fn.qualified_name
        assert fn.kind == orig_fn.kind
        assert fn.is_async == orig_fn.is_async
        assert fn.return_type == orig_fn.return_type

        # Parameter sentinels
        params = fn.parameters
        assert params[0].default == 42
        from smarter_mcp.extractor.models import _MISSING_TYPE, _NON_LITERAL_TYPE
        assert isinstance(params[1].default, _MISSING_TYPE)
        assert isinstance(params[2].default, _NON_LITERAL_TYPE)

        # Classes
        assert len(recovered.classes) == 1
        cls = recovered.classes[0]
        assert cls.name == "MyClass"
        assert cls.bases == ["Base"]
        assert len(cls.methods) == 1

    def test_disk_cache_round_trip(self, tmp_path):
        """Module written to disk must be recovered identically."""
        cache = ExtractionCache(cache_dir=tmp_path)
        mod = _make_sample_module()

        source = "def my_func(): pass"
        cache.put(source, "mymod", use_inspect=False, module=mod)

        result = cache.get(source, "mymod", use_inspect=False)
        assert result is not None
        assert result.module_name == "mymod"
        assert len(result.functions) == 1

    def test_cache_files_are_json_not_pickle(self, tmp_path):
        """Cache files must be .json, not .pkl."""
        cache = ExtractionCache(cache_dir=tmp_path)
        mod = _make_sample_module()
        cache.put("source", "mymod", use_inspect=False, module=mod)

        pkl_files = list(tmp_path.glob("**/*.pkl"))
        json_files = list(tmp_path.glob("**/*.json"))
        assert not pkl_files, f"Found .pkl files — pickle must not be used: {pkl_files}"
        assert json_files, "Expected at least one .json cache file"

    def test_corrupt_file_is_a_miss_not_an_exception(self, tmp_path):
        """A corrupt cache entry must degrade to a miss, not raise."""
        cache = ExtractionCache(cache_dir=tmp_path)
        mod = _make_sample_module()
        source = "def foo(): ..."

        cache.put(source, "mod2", use_inspect=False, module=mod)

        # Corrupt the file on disk
        json_files = list(tmp_path.glob("*.json"))
        assert json_files
        json_files[0].write_text("{{not valid json!!")

        # Miss, not exception
        cache._mem.clear()
        result = cache.get(source, "mod2", use_inspect=False)
        assert result is None

    def test_source_root_anchors_cache_dir(self, tmp_path):
        """When source_root is given (no explicit cache_dir), cache lives under it."""
        source_root = tmp_path / "my_project"
        source_root.mkdir()
        cache = ExtractionCache(source_root=source_root)

        # The cache dir must be under source_root
        assert str(cache.cache_dir).startswith(str(source_root))

    def test_cache_miss_on_different_source_content(self, tmp_path):
        """Changing the source content must result in a cache miss."""
        cache = ExtractionCache(cache_dir=tmp_path)
        mod = _make_sample_module()
        cache.put("original source", "mymod", use_inspect=False, module=mod)

        result = cache.get("different source", "mymod", use_inspect=False)
        assert result is None


class TestNoPikleImport:
    def test_no_pickle_import_in_cache_module(self):
        """The cache module must not import pickle anywhere (H5 security fix)."""
        import inspect

        import smarter_mcp.extractor.cache as cache_module

        source = inspect.getsource(cache_module)
        assert "import pickle" not in source, (
            "cache.py must not import pickle — use JSON serialisation instead"
        )
        assert "pickle.loads" not in source
        assert "pickle.dumps" not in source
