import os

import pytest

from agent.llm_client import ConfigurationError, LLMClient

_PLACEHOLDER_KEYS = {"", "your-key-here"}


@pytest.fixture
def llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-4o")


def test_raises_configuration_error_when_all_vars_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ConfigurationError, match="Missing required environment variables"):
        LLMClient()


def test_raises_configuration_error_lists_only_missing_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")

    with pytest.raises(
        ConfigurationError,
        match="Missing required environment variables: LLM_API_KEY, LLM_MODEL",
    ):
        LLMClient()


def test_raises_configuration_error_for_invalid_provider(
    llm_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")

    with pytest.raises(ConfigurationError, match="LLM_PROVIDER must be 'openrouter'"):
        LLMClient()


def test_client_exposes_model_and_openai_client(llm_env: None) -> None:
    client = LLMClient()

    assert client.provider == "openrouter"
    assert client.model == "openai/gpt-4o"
    assert client.get_openai_client() is not None


def test_env_alias_resolves_to_slug(
    llm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An LLM_MODEL alias is resolved to its OpenRouter slug."""
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-flash")
    assert LLMClient().model == "deepseek/deepseek-v4-flash"


def test_constructor_model_overrides_env(llm_env: None) -> None:
    """An explicit model override beats the env default (hot-swap)."""
    client = LLMClient(model="deepseek-v4-flash")
    assert client.model == "deepseek/deepseek-v4-flash"

    client_raw = LLMClient(model="anthropic/claude-3.5-sonnet")
    assert client_raw.model == "anthropic/claude-3.5-sonnet"


@pytest.mark.integration
def test_ping_returns_true() -> None:
    api_key = os.environ.get("LLM_API_KEY", "")
    if api_key in _PLACEHOLDER_KEYS:
        pytest.skip("LLM_API_KEY is not set or is still the placeholder value")

    client = LLMClient()
    assert client.ping() is True
