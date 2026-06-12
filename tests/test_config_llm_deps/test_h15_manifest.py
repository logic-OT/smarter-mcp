"""Tests for H15 — extra="forbid" on all manifest config models."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from smarter_mcp.config.manifest import (
    ExposeConfig,
    InstanceConfig,
    LLMConfig,
    ManifestConfig,
    MultimodalConfig,
    RoutingConfig,
    ServerConfig,
    SourceConfig,
    ToolOverride,
    load_manifest,
)


class TestExtraForbid:
    def test_server_config_rejects_unknown_key(self):
        with pytest.raises(ValidationError, match="bogus_key"):
            ServerConfig(bogus_key="oops")

    def test_source_config_rejects_unknown_key(self):
        with pytest.raises(ValidationError, match="unknown_field"):
            SourceConfig(path=".", unknown_field=True)

    def test_routing_config_rejects_unknown_key(self):
        with pytest.raises(ValidationError):
            RoutingConfig(totally_unknown=True)

    def test_expose_config_rejects_unknown_key(self):
        with pytest.raises(ValidationError, match="bogus"):
            ExposeConfig(bogus=True)

    def test_instance_config_rejects_unknown_key(self):
        with pytest.raises(ValidationError, match="typo_field"):
            InstanceConfig(class_name="Foo", typo_field=1)

    def test_tool_override_rejects_param_descriptions(self):
        """param_descriptions was removed; must now fail with extra=forbid."""
        with pytest.raises(ValidationError):
            ToolOverride(function="foo.bar", param_descriptions={"x": "desc"})

    def test_multimodal_config_rejects_image_format(self):
        """image_format was removed; must now fail with extra=forbid."""
        with pytest.raises(ValidationError):
            MultimodalConfig(image_format="jpeg")

    def test_llm_config_rejects_unknown_key(self):
        with pytest.raises(ValidationError, match="typo_key"):
            LLMConfig(typo_key="bad")

    def test_manifest_config_rejects_unknown_key(self):
        with pytest.raises(ValidationError, match="totally_made_up"):
            ManifestConfig(totally_made_up=True)

    def test_manifest_dir_still_settable_after_construction(self, tmp_path):
        """manifest_dir is Field(exclude=True) — setting it post-construction must work."""
        cfg = ManifestConfig()
        cfg.manifest_dir = str(tmp_path)  # must not raise
        assert cfg.manifest_dir == str(tmp_path)

    def test_load_manifest_rejects_yaml_with_unknown_key(self, tmp_path):
        """A YAML file with a bogus top-level key must raise ValidationError naming it."""
        mf = tmp_path / "smarter-mcp.yaml"
        mf.write_text("name: test\ncompletely_bogus_key: 99\n")
        with pytest.raises(ValidationError, match="completely_bogus_key"):
            load_manifest(str(mf))

    def test_load_manifest_rejects_readme_wrong_expose_key(self, tmp_path):
        """README-documented-but-wrong key 'private' inside expose: must fail."""
        mf = tmp_path / "smarter-mcp.yaml"
        mf.write_text(
            "name: t\n"
            "expose:\n"
            "  private: false\n"  # README says this; real key is include_private
        )
        with pytest.raises(ValidationError, match="private"):
            load_manifest(str(mf))


class TestRemovedFields:
    def test_cors_origins_removed(self):
        """cors_origins is removed; ServerConfig must not have the attribute."""
        sc = ServerConfig()
        assert not hasattr(sc, "cors_origins"), (
            "cors_origins should have been removed from ServerConfig"
        )

    def test_routing_base_path_removed(self):
        rc = RoutingConfig()
        assert not hasattr(rc, "base_path")

    def test_routing_root_aggregate_removed(self):
        rc = RoutingConfig()
        assert not hasattr(rc, "root_aggregate")

    def test_multimodal_image_format_removed(self):
        mc = MultimodalConfig()
        assert not hasattr(mc, "image_format")

    def test_tool_override_param_descriptions_removed(self):
        to = ToolOverride(function="foo.bar")
        assert not hasattr(to, "param_descriptions")


class TestKeptFields:
    def test_log_level_still_exists(self):
        sc = ServerConfig()
        assert hasattr(sc, "log_level")
        assert sc.log_level == "info"

    def test_auto_detect_still_exists(self):
        mc = MultimodalConfig()
        assert hasattr(mc, "auto_detect")
        assert mc.auto_detect is True

    def test_image_max_size_still_exists(self):
        mc = MultimodalConfig()
        assert hasattr(mc, "image_max_size")

    def test_routing_overrides_and_separator_still_exist(self):
        rc = RoutingConfig()
        assert hasattr(rc, "overrides")
        assert hasattr(rc, "separator")
