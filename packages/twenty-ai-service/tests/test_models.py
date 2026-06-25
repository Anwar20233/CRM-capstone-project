"""Tests for the model registry (agent/models.py)."""

import pytest

from agent.models import (
    DEFAULT_MODEL_ALIAS,
    MODEL_REGISTRY,
    ConfigurationError,
    list_models,
    resolve_model,
)


class TestResolveModel:
    def test_known_alias_resolves_to_slug(self) -> None:
        spec = resolve_model("deepseek-v4-flash")
        assert spec.id == "deepseek/deepseek-v4-flash"
        assert spec.label == "DeepSeek V4 Flash"

    def test_raw_slug_passes_through(self) -> None:
        spec = resolve_model("anthropic/claude-3.5-sonnet")
        assert spec.id == "anthropic/claude-3.5-sonnet"
        assert spec.label == "anthropic/claude-3.5-sonnet"

    def test_none_falls_back_to_default(self) -> None:
        spec = resolve_model(None)
        assert spec.id == MODEL_REGISTRY[DEFAULT_MODEL_ALIAS].id

    def test_empty_string_falls_back_to_default(self) -> None:
        assert resolve_model("").id == MODEL_REGISTRY[DEFAULT_MODEL_ALIAS].id

    def test_unknown_bare_name_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="Unknown model"):
            resolve_model("not-a-real-model")


class TestDefaultAndListing:
    def test_default_alias_is_deepseek_v4_flash(self) -> None:
        assert DEFAULT_MODEL_ALIAS == "deepseek-v4-flash"

    def test_list_models_shape(self) -> None:
        models = list_models()
        assert {"alias", "id", "label"} <= set(models[0])
        assert any(m["id"] == "deepseek/deepseek-v4-flash" for m in models)
