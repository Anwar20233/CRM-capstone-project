"""OpenRouter LLM client and structured JSON helper for the Python Worker framework.

Reads ``LLM_*`` configuration from the process environment (or a local ``.env``
loaded once at import) and exposes an OpenAI-compatible client pointed at
OpenRouter or OpenAI, and structured JSON call helpers.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TypeVar

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel

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

ModelT = TypeVar("ModelT", bound=BaseModel)


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
        raw_client = OpenAI(base_url=self._base_url, api_key=self._api_key)
        # Wrap with LangSmith auto-tracing (no-op when tracing is disabled).
        from tracing import wrap_client
        self._client = wrap_client(raw_client)

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


class LLMCallError(Exception):
    """Raised when the underlying LLM call or response parsing fails."""


def _resolve_chat_model(model: str | None = None):
    """Build a LangChain chat model from environment configuration.

    ``model`` (alias or raw OpenRouter slug) overrides the env default
    (``LLM_MODEL``) — this is how a subagent runs on its own model independent
    of the orchestrator (e.g. the follow-up subagents on Qwen while the
    orchestrator reasons on a smarter model).
    """
    try:
        config = _load_config()
        provider = config["provider"]
        model_name = model or config["model"]
        base_url = config["base_url"]
        api_key = config["api_key"]
    except ConfigurationError as exc:
        # Fall back to env variables directly if structured configuration fails
        provider = os.environ.get("LLM_PROVIDER", "openai").lower()
        model_name = model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
        base_url = os.environ.get("LLM_BASE_URL")
        api_key = os.environ.get("LLM_API_KEY")

    spec = resolve_model(model_name)
    model_id = spec.id

    if provider in ("openai", "openrouter"):
        from langchain_openai import ChatOpenAI

        if provider == "openai" and model_id.startswith("openai/"):
            model_id = model_id.split("/", 1)[1]

        return ChatOpenAI(
            model=model_id,
            temperature=0,
            api_key=api_key,
            base_url=base_url,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model_id,
            temperature=0,
            api_key=api_key,
            base_url=base_url,
        )

    raise LLMCallError(f"Unsupported LLM_PROVIDER '{provider}'")


async def call_llm_json(
    prompt: str, schema: type[ModelT], *, model: str | None = None
) -> ModelT:
    """Call the configured chat model and parse the response as `schema`.

    Args:
        prompt: Fully-formed prompt string (system + context + instructions).
        schema: Pydantic model the JSON response must conform to.
        model: Optional alias/slug overriding ``LLM_MODEL`` — lets a caller
            (e.g. a follow-up subagent) run on its own model.

    Returns:
        An instance of `schema` populated from the model's structured output.

    Raises:
        LLMCallError: If the model call fails or the response cannot be
            parsed into `schema`.
    """
    try:
        chat_model = _resolve_chat_model(model)
        structured_model = chat_model.with_structured_output(schema)
        result = await structured_model.ainvoke(prompt)
    except Exception as exc:  # noqa: BLE001 - normalize all provider errors
        raise LLMCallError(f"LLM call failed: {exc}") from exc

    if isinstance(result, schema):
        return result

    try:
        return schema.model_validate(result)
    except Exception as exc:  # noqa: BLE001
        raise LLMCallError(f"Failed to parse LLM response as {schema.__name__}: {exc}") from exc
