"""Shared configuration for the optimization harness.

Loads the normal ``.env`` then layers ``.env.training`` on top (override=True),
then configures the DSPy task LM and builds the GEPA reflection LM. Provider is
driven by ``LLM_PROVIDER``: ``openrouter`` (default — DeepSeek Flash) or ``openai``.
The DSPy LMs reuse the same key the worker uses (OpenRouter key in ``LLM_API_KEY``,
or ``OPENAI_API_KEY`` when routing through OpenAI directly).

Call ``configure()`` once at the start of every run script.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent  # packages/twenty-ai-service
_ENV = _SERVICE_ROOT / ".env"
_ENV_TRAINING = _SERVICE_ROOT / ".env.training"

# Defaults assume OpenRouter / DeepSeek Flash (litellm "openrouter/<slug>" format).
_DEFAULT_TASK_MODEL = "openrouter/deepseek/deepseek-v4-flash"
_DEFAULT_REFLECTION_MODEL = "openrouter/deepseek/deepseek-v4-flash"


def load_env() -> None:
    """Load .env then .env.training (training wins) and wire the worker's key."""
    load_dotenv(_ENV, override=False)
    if _ENV_TRAINING.exists():
        load_dotenv(_ENV_TRAINING, override=True)

    # The worker's LLMClient reads LLM_API_KEY. When routing through OpenAI
    # directly, force it to OPENAI_API_KEY (must overwrite, not setdefault — .env
    # already populated LLM_API_KEY with the OpenRouter key). For OpenRouter we
    # leave LLM_API_KEY as-is (the OpenRouter key from .env).
    if os.environ.get("LLM_PROVIDER") == "openai":
        openai_key = os.environ.get("OPENAI_API_KEY")
        if openai_key:
            os.environ["LLM_API_KEY"] = openai_key


def reflection_model() -> str:
    return os.environ.get("DSPY_REFLECTION_MODEL", _DEFAULT_REFLECTION_MODEL)


def task_model() -> str:
    return os.environ.get("DSPY_TASK_MODEL", _DEFAULT_TASK_MODEL)


def dspy_api_key() -> str:
    """Key for the DSPy LMs — OpenAI key when routing through OpenAI, else the
    OpenRouter key (LLM_API_KEY / OPENROUTER_API_KEY)."""
    if os.environ.get("LLM_PROVIDER") == "openai":
        key = os.environ.get("OPENAI_API_KEY")
    else:
        key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key:
        raise RuntimeError("No API key available for the DSPy LMs (set LLM_API_KEY/OPENAI_API_KEY).")
    return key


def configure() -> None:
    """Load env and configure the DSPy global (task) LM. Idempotent."""
    import dspy

    load_env()
    lm = dspy.LM(task_model(), api_key=dspy_api_key(), temperature=1.0, max_tokens=4000)
    dspy.configure(lm=lm)


def build_reflection_lm():
    """The LM GEPA uses to *rewrite* the prompt."""
    import dspy

    return dspy.LM(
        reflection_model(),
        api_key=dspy_api_key(),
        temperature=1.0,
        max_tokens=32000,
    )
