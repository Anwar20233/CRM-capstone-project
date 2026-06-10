"""OpenRouter LLM client for the Python Worker framework.

Reads ``LLM_*`` configuration from the process environment (or a local ``.env``
loaded once at import) and exposes an OpenAI-compatible client pointed at
OpenRouter. The active model is resolved through ``agent.models``, so callers can
hot-swap models by alias without touching env — ``LLMClient(model="gpt-4o")``.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from agent.models import ConfigurationError, ModelSpec, resolve_model

# Load packages/twenty-ai-service/.env once. ``override=False`` so explicitly
# exported env vars and test monkeypatching always win over the file.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

_ENV_VARS = (
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
)


class LLMClient:
    """OpenAI-compatible client configured for OpenRouter.

    Parameters
    ----------
    model:
        Optional alias or OpenRouter slug overriding the env default
        (``LLM_MODEL``). Resolved through ``agent.models.resolve_model``.
    """

    def __init__(self, model: str | None = None) -> None:
        config = _load_config()
        self._provider = config["provider"]
        self._base_url = config["base_url"]
        self._api_key = config["api_key"]
        # Explicit override > env default; both flow through the registry.
        self._model_spec: ModelSpec = resolve_model(model or config["model"])
        # Direct OpenAI expects bare ids ("gpt-4o-mini"), not OpenRouter-style
        # "openai/" slugs. Strip the prefix when talking to OpenAI directly.
        model_id = self._model_spec.id
        if self._provider == "openai" and model_id.startswith("openai/"):
            model_id = model_id.split("/", 1)[1]
        self._model_id = model_id
        self._client = OpenAI(base_url=self._base_url, api_key=self._api_key)

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        """The model id passed to the API (bare id for OpenAI, slug for OpenRouter)."""
        return self._model_id

    @property
    def model_spec(self) -> ModelSpec:
        return self._model_spec

    def get_openai_client(self) -> OpenAI:
        """Return the underlying OpenAI SDK client."""
        return self._client

    def ping(self) -> bool:
        """Send a hello message and return True if the response is non-empty."""
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": "Hello"}],
        )
        content = response.choices[0].message.content
        return bool(content and content.strip())


def _load_config() -> dict[str, str]:
    """Read and validate ``LLM_*`` environment variables."""
    values = {name: os.environ.get(name) for name in _ENV_VARS}

    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigurationError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    provider = values["LLM_PROVIDER"]
    if provider not in ("openrouter", "openai"):
        raise ConfigurationError(
            f"LLM_PROVIDER must be 'openrouter' or 'openai', got {provider!r}"
        )

    return {
        "provider": provider,
        "base_url": values["LLM_BASE_URL"],
        "api_key": values["LLM_API_KEY"],
        "model": values["LLM_MODEL"],
    }
