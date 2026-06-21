"""Token metering for the follow-up eval — live trace + end-of-run report.

A thread-safe meter plus a LangChain callback handler that reads the token usage
off every LLM response. Attach the callback to each chat model (the eval already
wraps models to add a prompt-capture handler; we compose alongside it), get a
concise live line per call, and a per-model breakdown at the end.

No external service — usage comes straight from the provider response, so it
works the same whether routed at OpenAI directly or via OpenRouter.
"""

from __future__ import annotations

import sys
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

# Optional, clearly-labelled price table ($ per 1M tokens) for a rough cost line.
# Unknown models simply omit cost. Edit/extend as needed.
_PRICE_PER_M = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    # gpt-5.4* pricing unknown here — left out so we never report a wrong number.
}


@dataclass
class _ModelTally:
    calls: int = 0
    prompt: int = 0
    completion: int = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion


@dataclass
class TokenMeter:
    by_model: dict[str, _ModelTally] = field(default_factory=lambda: defaultdict(_ModelTally))
    calls: int = 0
    prompt: int = 0
    completion: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    live: bool = True

    def record(self, model: str, prompt: int, completion: int) -> None:
        with self._lock:
            self.calls += 1
            self.prompt += prompt
            self.completion += completion
            tally = self.by_model[model]
            tally.calls += 1
            tally.prompt += prompt
            tally.completion += completion
            if self.live:
                print(
                    f"[tok] {model:<22} +{prompt:>6} in / +{completion:>5} out  "
                    f"| Σ {self.prompt + self.completion:>8,} ({self.calls} calls)",
                    file=sys.stderr, flush=True,
                )

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def report(self) -> str:
        lines = [
            "",
            "=" * 70,
            "  TOKEN USAGE",
            "=" * 70,
            f"  LLM calls            : {self.calls}",
            f"  prompt (input)       : {self.prompt:,}",
            f"  completion (output)  : {self.completion:,}",
            f"  TOTAL                : {self.total:,}",
            "",
            "  per model:",
        ]
        total_cost = 0.0
        any_cost = False
        for model in sorted(self.by_model):
            t = self.by_model[model]
            line = (f"    {model:<24} calls={t.calls:<4} in={t.prompt:>8,} "
                    f"out={t.completion:>7,} total={t.total:>8,}")
            price = _PRICE_PER_M.get(model)
            if price:
                any_cost = True
                cost = t.prompt / 1e6 * price[0] + t.completion / 1e6 * price[1]
                total_cost += cost
                line += f"  ~${cost:,.4f}"
            lines.append(line)
        if any_cost:
            lines.append(f"\n  estimated cost (known models only): ~${total_cost:,.4f}")
        else:
            lines.append("\n  (no price table for these models — tokens only)")
        lines.append("=" * 70)
        return "\n".join(lines)


def _extract_usage(response: Any) -> tuple[str, int, int]:
    """Pull (model, prompt_tokens, completion_tokens) from an LLMResult."""
    model = "unknown"
    prompt = completion = 0
    llm_output = getattr(response, "llm_output", None) or {}
    if isinstance(llm_output, dict):
        model = llm_output.get("model_name") or llm_output.get("model") or model
        usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
        if isinstance(usage, dict):
            prompt = usage.get("prompt_tokens", 0) or 0
            completion = usage.get("completion_tokens", 0) or 0

    # Fallback / supplement: sum usage_metadata off each generated message.
    if not (prompt or completion):
        for batch in getattr(response, "generations", []) or []:
            for gen in batch:
                message = getattr(gen, "message", None)
                meta = getattr(message, "usage_metadata", None) if message else None
                if meta:
                    prompt += meta.get("input_tokens", 0) or 0
                    completion += meta.get("output_tokens", 0) or 0
                    rd = meta.get("model_name") if isinstance(meta, dict) else None
                    if rd:
                        model = rd
    return model, prompt, completion


class TokenMeterCallback(BaseCallbackHandler):
    """LangChain callback that records token usage on every LLM completion."""

    def __init__(self, meter: TokenMeter) -> None:
        self._meter = meter

    def on_llm_end(self, response: Any, **_kwargs: Any) -> None:  # noqa: ANN401
        model, prompt, completion = _extract_usage(response)
        self._meter.record(model, prompt, completion)


# A single process-wide meter the eval wrapper installs and reports from.
METER = TokenMeter()


def attach(model: Any) -> Any:
    """Append the token-meter callback to a chat model (idempotent-ish)."""
    handler = TokenMeterCallback(METER)
    existing = list(getattr(model, "callbacks", None) or [])
    if not any(isinstance(h, TokenMeterCallback) for h in existing):
        existing.append(handler)
        try:
            model.callbacks = existing
        except Exception:  # noqa: BLE001 — some models freeze attrs; best effort
            return model.with_config(callbacks=[handler])
    return model
