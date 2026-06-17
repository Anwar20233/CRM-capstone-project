"""Shared structured-JSON LLM call helper (Person 1 infra).

NOTE: Minimal placeholder so that intelligence agents (Next Step, Risk,
Drafting) can be written against a stable `call_llm_json` signature ahead of
Person 1's final LLM infrastructure. Agents should treat this function as an
external dependency and mock it in unit tests.

The implementation lazily resolves a LangChain chat model from environment
configuration and uses structured output to coerce the response into the
given Pydantic model.
"""

from __future__ import annotations

import os
from typing import TypeVar

from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)


class LLMCallError(Exception):
    """Raised when the underlying LLM call or response parsing fails."""


def _resolve_chat_model():
    """Build a LangChain chat model from `LLM_PROVIDER` / `LLM_MODEL` env vars."""
    provider = os.environ.get("LLM_PROVIDER", "openai").lower()
    model_name = os.environ.get("LLM_MODEL", "gpt-4o-mini")

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model_name, temperature=0)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model_name, temperature=0)

    raise LLMCallError(f"Unsupported LLM_PROVIDER '{provider}'")


async def call_llm_json(prompt: str, schema: type[ModelT]) -> ModelT:
    """Call the configured chat model and parse the response as `schema`.

    Args:
        prompt: Fully-formed prompt string (system + context + instructions).
        schema: Pydantic model the JSON response must conform to.

    Returns:
        An instance of `schema` populated from the model's structured output.

    Raises:
        LLMCallError: If the model call fails or the response cannot be
            parsed into `schema`.
    """
    try:
        model = _resolve_chat_model()
        structured_model = model.with_structured_output(schema)
        result = await structured_model.ainvoke(prompt)
    except Exception as exc:  # noqa: BLE001 - normalize all provider errors
        raise LLMCallError(f"LLM call failed: {exc}") from exc

    if isinstance(result, schema):
        return result

    try:
        return schema.model_validate(result)
    except Exception as exc:  # noqa: BLE001
        raise LLMCallError(f"Failed to parse LLM response as {schema.__name__}: {exc}") from exc
