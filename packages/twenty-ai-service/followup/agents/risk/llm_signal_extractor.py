"""LLM evidence extraction for the Risk Agent.

The LLM reads compact CRM text and extracts structured risk signals before
deterministic scoring runs. It never calculates the score, risk level, alert
urgency, or notification decision.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from agent.llm_client import LLMClient

logger = logging.getLogger(__name__)

_ALLOWED_FACT_TYPES = {
    "concern",
    "gate",
    "objection",
    "blocker",
    "delay",
    "process",
    "budget",
    "sentiment",
    "buying_signal",
}
_ALLOWED_SENTIMENTS = {"negative", "neutral", "positive"}
_MAX_EVIDENCE_ITEMS = 8
_MAX_TEXT_CHARS = 320
_MAX_SIGNALS = 6
_LLM_TIMEOUT_SECONDS = 12


async def extract_llm_risk_signals(
    *,
    opportunity_name: str | None,
    profile_narrative: str | None = None,
    recent_messages: list[dict[str, Any]] | None = None,
    recent_notes: list[dict[str, Any]] | None = None,
    recent_tasks: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    evidence = _build_evidence(
        profile_narrative=profile_narrative,
        recent_messages=recent_messages or [],
        recent_notes=recent_notes or [],
        recent_tasks=recent_tasks or [],
    )
    if not evidence:
        return []

    prompt = _build_prompt(
        opportunity_name=opportunity_name,
        evidence=evidence,
    )

    try:
        logger.info("Extracting LLM risk signals")
        content = await asyncio.wait_for(
            _call_llm_signal_extractor(prompt),
            timeout=_LLM_TIMEOUT_SECONDS,
        )
        return _parse_signal_response(content)
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "LLM risk signal extraction failed; continuing without LLM signals: %s",
            type(error).__name__,
        )
        return []


async def _call_llm_signal_extractor(prompt: str) -> str:
    client = LLMClient()
    openai_client = client.get_openai_client()

    def create_completion() -> str:
        response = openai_client.chat.completions.create(
            model=client.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=600,
        )
        content = response.choices[0].message.content
        return content if isinstance(content, str) else ""

    return await asyncio.to_thread(create_completion)


def _build_prompt(
    *,
    opportunity_name: str | None,
    evidence: list[dict[str, str]],
) -> str:
    return f"""You extract structured CRM deal risk signals for deterministic scoring.

The scoring system will calculate the risk score later. Do not calculate a risk score,
risk level, urgency, or notification decision.

Rules:
- Use only the evidence provided.
- Do not invent facts.
- Extract at most {_MAX_SIGNALS} signals.
- Return valid JSON only.
- Each signal must use one of these fact_type values: {sorted(_ALLOWED_FACT_TYPES)}.
- sentiment must be one of: {sorted(_ALLOWED_SENTIMENTS)}.
- confidence must be a number between 0 and 1.
- Use buying_signal only for positive momentum.

Input:
Opportunity name: {_safe_text(opportunity_name)}
Evidence: {json.dumps(evidence, default=str)}

Return this JSON shape:
{{
  "signals": [
    {{
      "fact_type": "objection",
      "fact_value": "Budget owner wants a lower price",
      "sentiment": "negative",
      "confidence": 0.8,
      "source_snippet": "budget owner wants below $25k"
    }}
  ]
}}"""


def _build_evidence(
    *,
    profile_narrative: str | None,
    recent_messages: list[dict[str, Any]],
    recent_notes: list[dict[str, Any]],
    recent_tasks: list[dict[str, Any]],
) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []

    if profile_narrative:
        evidence.append(
            {
                "source_type": "profile_narrative",
                "text": _truncate(profile_narrative),
            }
        )

    for message in recent_messages[:_MAX_EVIDENCE_ITEMS]:
        evidence.append(
            {
                "source_type": "message",
                "text": _truncate(
                    " ".join(
                        [
                            _safe_text(message.get("subject")),
                            _safe_text(message.get("text")),
                        ]
                    )
                ),
            }
        )

    for note in recent_notes[:_MAX_EVIDENCE_ITEMS]:
        evidence.append(
            {
                "source_type": "note",
                "text": _truncate(
                    " ".join(
                        [
                            _safe_text(note.get("title")),
                            _safe_text(note.get("body")),
                        ]
                    )
                ),
            }
        )

    for task in recent_tasks[:_MAX_EVIDENCE_ITEMS]:
        evidence.append(
            {
                "source_type": "task",
                "text": _truncate(
                    " ".join(
                        [
                            _safe_text(task.get("title")),
                            _safe_text(task.get("body")),
                            f"status={_safe_text(task.get('status'))}",
                        ]
                    )
                ),
            }
        )

    return [
        item
        for item in evidence[:_MAX_EVIDENCE_ITEMS]
        if item["text"] and item["text"] != "Not provided"
    ]


def _parse_signal_response(content: str) -> list[dict[str, Any]]:
    if not content.strip():
        return []

    data = json.loads(_strip_code_fence(content))
    raw_signals = data.get("signals", []) if isinstance(data, dict) else []
    if not isinstance(raw_signals, list):
        return []

    signals: list[dict[str, Any]] = []
    for raw_signal in raw_signals[:_MAX_SIGNALS]:
        if not isinstance(raw_signal, dict):
            continue

        signal = _normalize_signal(raw_signal)
        if signal is not None:
            signals.append(signal)

    return signals


def _normalize_signal(raw_signal: dict[str, Any]) -> dict[str, Any] | None:
    fact_type = _safe_text(raw_signal.get("fact_type")).lower()
    fact_value = _safe_text(raw_signal.get("fact_value"))
    sentiment = _safe_text(raw_signal.get("sentiment")).lower() or "neutral"
    source_snippet = _safe_text(raw_signal.get("source_snippet"))

    if fact_type not in _ALLOWED_FACT_TYPES or not fact_value:
        return None
    if sentiment not in _ALLOWED_SENTIMENTS:
        sentiment = "neutral"

    try:
        confidence = float(raw_signal.get("confidence") or 0.7)
    except (TypeError, ValueError):
        confidence = 0.7

    confidence = max(0.0, min(confidence, 1.0))

    return {
        "fact_type": fact_type,
        "fact_value": fact_value[:_MAX_TEXT_CHARS],
        "confidence": confidence,
        "sentiment": sentiment,
        "source_type": "llm_risk_signal",
        "source_id": "llm_risk_signal",
        "source_snippet": source_snippet[:_MAX_TEXT_CHARS] or fact_value[:_MAX_TEXT_CHARS],
    }


def _strip_code_fence(content: str) -> str:
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _safe_text(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else "Not provided"


def _truncate(value: Any) -> str:
    return str(value or "").strip()[:_MAX_TEXT_CHARS]
