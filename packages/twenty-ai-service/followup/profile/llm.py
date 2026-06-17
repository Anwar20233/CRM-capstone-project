"""LangChain chat model factory for the extraction pipeline.

The pipeline's two LLM stages (mention identification, then fact extraction) run
as LangGraph nodes, and each node talks to the model through a LangChain
``ChatOpenAI`` instance. We deliberately reuse ``agent.llm_client._load_config``
and ``agent.models.resolve_model`` so the follow-up agent reads the SAME
``LLM_*`` environment the rest of twenty-ai-service uses — one provider, one key,
one model registry. The only difference is the surface: a LangChain chat model
(what these LangGraph nodes expect) instead of the raw OpenAI client the worker
loop uses.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage

from agent.llm_client import _load_config
from agent.models import resolve_model

# Extraction must be reproducible and literal — no creative sampling.
_DEFAULT_TEMPERATURE = 0.0


class _RequestDumpHandler(BaseCallbackHandler):
    """Dump the exact messages crossing the wire to the LLM, masking included.

    Opt-in via ``LLM_TRACE_REQUESTS``: ``stderr`` (or ``1``/``true``) prints to
    stderr; any other value is a file path to append JSONL to. This is the
    ground truth of what each followup node (extract, classify, synthesis) sends
    *after* masking — so it shows handles (person001), not raw PII. The plain
    text the LLM never sees (raw trigger/state) lives in the LangGraph node spans,
    not here. No-op when the env var is unset.
    """

    def on_chat_model_start(
        self, serialized: dict, messages: list[list[BaseMessage]], **kwargs: Any
    ) -> None:
        target = os.environ.get("LLM_TRACE_REQUESTS")
        if not target:
            return
        flat = [
            {"role": m.type, "content": m.content}
            for batch in messages
            for m in batch
        ]
        from followup.profile.masking import models_available

        record = {
            "source": "followup",
            "models_loaded": models_available(),
            "messages": flat,
        }
        if target.lower() in ("1", "true", "stderr"):
            print(json.dumps(record, indent=2, default=str), file=sys.stderr)
        else:
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")

# Strips a ```json ... ``` (or bare ``` ... ```) fence some models wrap JSON in.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def build_chat_llm(
    model: str | None = None, temperature: float = _DEFAULT_TEMPERATURE
) -> BaseChatModel:
    """Build a LangChain chat model pointed at the configured LLM provider.

    ``model`` is an alias or raw OpenRouter slug overriding the env default
    (``LLM_MODEL``), resolved through the shared model registry.
    """
    # Imported lazily so importing the pipeline does not hard-require
    # langchain-openai for callers that inject their own chat model.
    from langchain_openai import ChatOpenAI

    config = _load_config()
    spec = resolve_model(model or config["model"])
    model_id = spec.id
    # Direct OpenAI expects bare ids; OpenRouter expects the "vendor/slug" form.
    if config["provider"] == "openai" and model_id.startswith("openai/"):
        model_id = model_id.split("/", 1)[1]

    return ChatOpenAI(
        model=model_id,
        base_url=config["base_url"],
        api_key=config["api_key"],
        temperature=temperature,
        callbacks=[_RequestDumpHandler()],
    )


def parse_json_response(content: str) -> dict[str, Any]:
    """Parse a model's text response into a JSON object.

    Tolerant of markdown code fences and of leading/trailing prose: falls back
    to the first ``{...}`` span if the whole string is not valid JSON. Returns
    an empty dict when nothing parseable is found, so a malformed response
    degrades to "extracted nothing" rather than crashing the run.
    """
    if not content:
        return {}

    stripped = _FENCE_RE.sub("", content.strip())
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass

    # Last resort: grab the outermost brace-delimited span and try again.
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(stripped[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
