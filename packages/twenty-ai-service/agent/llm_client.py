"""OpenRouter LLM client for the Python Worker framework.

Reads LLM_* environment variables and exposes an OpenAI-compatible client
pointed at OpenRouter's base URL.
"""

import os

from openai import OpenAI

_ENV_VARS = (
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
)


class ConfigurationError(Exception):
    """Raised when required LLM environment variables are missing or invalid."""


class LLMClient:
    """OpenAI-compatible client configured for OpenRouter."""

    def __init__(self) -> None:
        config = _load_config()
        self._provider = config["provider"]
        self._base_url = config["base_url"]
        self._api_key = config["api_key"]
        self._model = config["model"]
        self._client = OpenAI(base_url=self._base_url, api_key=self._api_key)

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    def get_openai_client(self) -> OpenAI:
        """Return the underlying OpenAI SDK client."""
        return self._client

    def ping(self) -> bool:
        """Send a hello message and return True if the response is non-empty."""
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": "Hello"}],
        )
        content = response.choices[0].message.content
        return bool(content and content.strip())


def _load_config() -> dict[str, str]:
    """Read and validate LLM_* environment variables."""
    values = {name: os.environ.get(name) for name in _ENV_VARS}

    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigurationError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    provider = values["LLM_PROVIDER"]
    if provider != "openrouter":
        raise ConfigurationError(
            f"LLM_PROVIDER must be 'openrouter', got {provider!r}"
        )

    return {
        "provider": provider,
        "base_url": values["LLM_BASE_URL"],
        "api_key": values["LLM_API_KEY"],
        "model": values["LLM_MODEL"],
    }
