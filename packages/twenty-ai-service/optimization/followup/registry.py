"""Registry of follow-up agents to optimize.

One ``AgentSpec`` per agent is the single extension point: it bundles the
carrier program, the dataset loader, the GEPA feedback metric, and the file +
symbol the winning prompt ports back into. ``run_all.py`` and ``gate.py`` iterate
this registry, so adding an agent is one entry here plus its three small modules
(program / dataset / metric) — exactly the next-step trio below.

All five follow-up agents are wired and runnable in isolation (no live bridge,
no DB, no RAG, no Presidio masking — datasets are synthetic / pre-masked).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import dspy


@dataclass(frozen=True)
class AgentSpec:
    name: str
    build_program: Callable[..., dspy.Module]   # (prompt=None, model=None, run_carrier=bool) -> Module
    load_dataset: Callable[[str | None], list[dspy.Example]]
    split_counts: Callable[[], dict[str, int]]
    metric: Callable[..., dspy.Prediction]
    seed_prompt: Callable[[], str]
    input_field: str                            # the dspy.Example input key
    # Where the optimized prompt ports back to (documentation + porting helper).
    port_back_file: str
    port_back_symbol: str


def _next_step_spec() -> AgentSpec:
    from optimization.followup.metric.next_step_metric import metric_with_feedback
    from optimization.followup.program.next_step_program import (
        NextStepProgram,
        load_dataset,
        seed_prompt,
        split_counts,
    )

    return AgentSpec(
        name="next_step",
        build_program=NextStepProgram,
        load_dataset=load_dataset,
        split_counts=split_counts,
        metric=metric_with_feedback,
        seed_prompt=seed_prompt,
        input_field="case_id",
        port_back_file="followup/next_step/agents/next_step/prompts.py",
        port_back_symbol="SYSTEM_PROMPT",
    )


def _drafting_spec() -> AgentSpec:
    from optimization.followup.metric.drafting_metric import metric_with_feedback
    from optimization.followup.program.drafting_program import (
        DraftingProgram, load_dataset, seed_prompt, split_counts,
    )

    return AgentSpec(
        name="drafting", build_program=DraftingProgram, load_dataset=load_dataset,
        split_counts=split_counts, metric=metric_with_feedback, seed_prompt=seed_prompt,
        input_field="case_id",
        port_back_file="followup/emailer/agents/drafting/prompts.py",
        port_back_symbol="DRAFTING_SYSTEM_PROMPT",
    )


def _extraction_spec() -> AgentSpec:
    from optimization.followup.metric.extraction_metric import metric_with_feedback
    from optimization.followup.program.extraction_program import (
        ExtractionProgram, load_dataset, seed_prompt, split_counts,
    )

    return AgentSpec(
        name="extraction", build_program=ExtractionProgram, load_dataset=load_dataset,
        split_counts=split_counts, metric=metric_with_feedback, seed_prompt=seed_prompt,
        input_field="case_id",
        port_back_file="followup/profile/prompts.py",
        port_back_symbol="EXTRACTION_INSTRUCTIONS",
    )


def _synthesis_spec() -> AgentSpec:
    from optimization.followup.metric.synthesis_metric import metric_with_feedback
    from optimization.followup.program.synthesis_program import (
        SynthesisProgram, load_dataset, seed_prompt, split_counts,
    )

    return AgentSpec(
        name="synthesis", build_program=SynthesisProgram, load_dataset=load_dataset,
        split_counts=split_counts, metric=metric_with_feedback, seed_prompt=seed_prompt,
        input_field="case_id",
        port_back_file="followup/profile/synthesis.py",
        port_back_symbol="_SYSTEM_PROMPT",
    )


def _chat_spec() -> AgentSpec:
    from optimization.followup.metric.chat_metric import metric_with_feedback
    from optimization.followup.program.chat_program import (
        ChatProgram, load_dataset, seed_prompt, split_counts,
    )

    return AgentSpec(
        name="chat", build_program=ChatProgram, load_dataset=load_dataset,
        split_counts=split_counts, metric=metric_with_feedback, seed_prompt=seed_prompt,
        input_field="case_id",
        port_back_file="followup/chat/agent.py",
        port_back_symbol="_SYSTEM_PROMPT",
    )


# Lazily built so importing the registry never imports every agent's deps.
_BUILDERS: dict[str, Callable[[], AgentSpec]] = {
    "next_step": _next_step_spec,
    "drafting": _drafting_spec,
    "extraction": _extraction_spec,
    "synthesis": _synthesis_spec,
    "chat": _chat_spec,
}


def available_agents() -> list[str]:
    return list(_BUILDERS)


def get_spec(name: str) -> AgentSpec:
    if name not in _BUILDERS:
        raise KeyError(
            f"Unknown agent '{name}'. Available: {', '.join(_BUILDERS)}. "
            "Other agents are scaffolded but not yet wired — see registry.py."
        )
    return _BUILDERS[name]()


def specs(names: list[str] | None = None) -> list[AgentSpec]:
    chosen = names or available_agents()
    return [get_spec(name) for name in chosen]
