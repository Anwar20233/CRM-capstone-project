"""Model registry — named presets so we can hot-swap LLMs.

Every entry maps a short **alias** to an OpenRouter model **slug**. Callers (the
worker loop, tests, and — later — a user-facing model picker) select a model by
alias without touching env vars or code:

    LLMClient(model="deepseek-v4-flash")    # by alias
    LLMClient(model="deepseek/deepseek-v4-flash")  # by raw slug (pass-through)
    LLMClient()                              # falls back to env LLM_MODEL

A raw slug (anything containing ``/``) is accepted as-is, so any OpenRouter model
works even if it isn't pre-registered. Edit ``MODEL_REGISTRY`` to add/remove the
curated presets.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ConfigurationError(Exception):
    """Raised when LLM configuration is missing or invalid."""


@dataclass(frozen=True)
class ModelSpec:
    """A selectable model: its OpenRouter slug, a human label, default params."""

    id: str  # OpenRouter slug, e.g. "deepseek/deepseek-v4-flash"
    label: str
    # Default sampling params (temperature, etc.) — reserved for future use.
    params: dict[str, object] = field(default_factory=dict)


# Curated presets. Add a line to expose a new model to selection/UI.
MODEL_REGISTRY: dict[str, ModelSpec] = {
    "deepseek-v4-flash": ModelSpec("deepseek/deepseek-v4-flash", "DeepSeek V4 Flash"),
    "deepseek-v4-pro": ModelSpec("deepseek/deepseek-v4-pro", "DeepSeek V4 Pro"),
    "qwen3-next-80b": ModelSpec(
        "qwen/qwen3-next-80b-a3b-instruct", "Qwen3 Next 80B A3B Instruct"
    ),
    "gpt-4o": ModelSpec("openai/gpt-4o", "OpenAI GPT-4o"),
    "gpt-4o-mini": ModelSpec("openai/gpt-4o-mini", "OpenAI GPT-4o mini"),
    # GPT-5.4 family — the faster "mini" is the follow-up runtime default after
    # prompt optimization; the full model is the GEPA reflection/proposer LM.
    "gpt-5.4": ModelSpec("openai/gpt-5.4", "OpenAI GPT-5.4"),
    "gpt-5.4-mini": ModelSpec("openai/gpt-5.4-mini", "OpenAI GPT-5.4 mini"),
    # Free OpenRouter tier — handy for burning-credit-free smoke tests.
    "gemma-free": ModelSpec("google/gemma-4-31b-it:free", "Google Gemma 4 31B (free)"),
}

# Used when neither the caller nor env specifies a model.
DEFAULT_MODEL_ALIAS = "deepseek-v4-flash"

# Follow-up agent model split: the orchestrator's own reasoning runs on a
# smarter/faster model; its subagents (next-step, drafting) run on a cheaper
# worker model. Both are overridable via env (see the bundle factory + deps).
FOLLOWUP_ORCHESTRATOR_MODEL_ALIAS = "deepseek-v4-flash"
FOLLOWUP_SUBAGENT_MODEL_ALIAS = "qwen3-next-80b"


def resolve_model(name: str | None) -> ModelSpec:
    """Resolve an alias or raw OpenRouter slug to a ``ModelSpec``.

    - A registered alias returns its spec.
    - A raw slug (contains ``/``) is accepted as-is.
    - ``None`` / empty falls back to ``DEFAULT_MODEL_ALIAS``.
    - Anything else raises ``ConfigurationError``.
    """
    if not name:
        name = DEFAULT_MODEL_ALIAS

    if name in MODEL_REGISTRY:
        return MODEL_REGISTRY[name]

    if "/" in name:
        return ModelSpec(id=name, label=name)

    raise ConfigurationError(
        f"Unknown model {name!r}. Use a registered alias "
        f"({', '.join(sorted(MODEL_REGISTRY))}) or a full OpenRouter slug "
        f"like 'deepseek/deepseek-v4-flash'."
    )


def list_models() -> list[dict[str, str]]:
    """Return the registered presets as ``[{alias, id, label}]`` — for a picker."""
    return [
        {"alias": alias, "id": spec.id, "label": spec.label}
        for alias, spec in MODEL_REGISTRY.items()
    ]
