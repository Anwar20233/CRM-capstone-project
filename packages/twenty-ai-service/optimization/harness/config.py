"""Shared configuration for the optimization harness.

Loads the normal ``.env`` then layers ``.env.training`` on top (override=True) so
the Writer worker runs directly on OpenAI during optimization, and configures the
DSPy task LM + builds the GEPA reflection LM — all from ``OPENAI_API_KEY``.

Call ``configure()`` once at the start of every run script.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent  # packages/twenty-ai-service
_ENV = _SERVICE_ROOT / ".env"
_ENV_TRAINING = _SERVICE_ROOT / ".env.training"

_DEFAULT_TASK_MODEL = "openai/gpt-4o-mini"
_DEFAULT_REFLECTION_MODEL = "openai/gpt-4.1"


def load_env() -> None:
    """Load .env then .env.training (training wins), and wire the OpenAI key."""
    load_dotenv(_ENV, override=False)
    if _ENV_TRAINING.exists():
        load_dotenv(_ENV_TRAINING, override=True)

    # The worker's LLMClient reads LLM_API_KEY; point it at the OpenAI key when
    # training routes through OpenAI directly.
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key and os.environ.get("LLM_PROVIDER") == "openai":
        os.environ.setdefault("LLM_API_KEY", openai_key)


def reflection_model() -> str:
    return os.environ.get("DSPY_REFLECTION_MODEL", _DEFAULT_REFLECTION_MODEL)


def task_model() -> str:
    return os.environ.get("DSPY_TASK_MODEL", _DEFAULT_TASK_MODEL)


def openai_api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set (needed for DSPy LMs).")
    return key


def configure() -> None:
    """Load env and configure the DSPy global (task) LM. Idempotent."""
    import dspy

    load_env()
    lm = dspy.LM(task_model(), api_key=openai_api_key(), temperature=1.0, max_tokens=4000)
    dspy.configure(lm=lm)


def build_reflection_lm():
    """The stronger LM GEPA uses to *rewrite* the prompt."""
    import dspy

    return dspy.LM(
        reflection_model(),
        api_key=openai_api_key(),
        temperature=1.0,
        max_tokens=32000,
    )
