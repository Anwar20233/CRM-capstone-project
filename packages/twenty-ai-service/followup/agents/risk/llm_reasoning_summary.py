"""
Hybrid LLM explanation layer for the Risk Agent.

The risk score remains deterministic and rule-based for reliability,
explainability, and testability.

The LLM is only used to convert the already-calculated risk factors into
a clearer sales-friendly reasoning summary.

It must never calculate or modify the score, risk level, or detected factors.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, is_dataclass
from typing import Any

from agent.llm_client import LLMClient

logger = logging.getLogger(__name__)

_MAX_FACTOR_COUNT = 5
_MAX_TEXT_CHARS = 240
_MAX_SUMMARY_CHARS = 900
_LLM_TIMEOUT_SECONDS = 12


async def generate_llm_reasoning_summary(
    *,
    opportunity_name: str | None,
    risk_score: int,
    risk_level: str,
    risk_factors: list,
    deterministic_summary: str,
    profile_narrative: str | None = None,
    recent_activity_summary: str | None = None,
) -> str:
    if not deterministic_summary.strip():
        return deterministic_summary

    prompt = _build_prompt(
        opportunity_name=opportunity_name,
        risk_score=risk_score,
        risk_level=risk_level,
        risk_factors=risk_factors,
        deterministic_summary=deterministic_summary,
        profile_narrative=profile_narrative,
        recent_activity_summary=recent_activity_summary,
    )

    try:
        logger.info("Generating LLM risk reasoning summary")
        summary = await asyncio.wait_for(
            _call_llm_reasoning_summary(prompt),
            timeout=_LLM_TIMEOUT_SECONDS,
        )
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "LLM risk reasoning summary failed; falling back to deterministic summary: %s",
            type(error).__name__,
        )
        return deterministic_summary

    cleaned_summary = summary.strip()
    if not cleaned_summary:
        logger.warning(
            "LLM risk reasoning summary returned empty text; falling back to deterministic summary"
        )
        return deterministic_summary

    return cleaned_summary[:_MAX_SUMMARY_CHARS]


async def _call_llm_reasoning_summary(prompt: str) -> str:
    client = LLMClient()
    openai_client = client.get_openai_client()

    def create_completion() -> str:
        response = openai_client.chat.completions.create(
            model=client.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=220,
        )
        content = response.choices[0].message.content
        return content if isinstance(content, str) else ""

    return await asyncio.to_thread(create_completion)


def _build_prompt(
    *,
    opportunity_name: str | None,
    risk_score: int,
    risk_level: str,
    risk_factors: list,
    deterministic_summary: str,
    profile_narrative: str | None,
    recent_activity_summary: str | None,
) -> str:
    compact_factors = _compact_risk_factors(risk_factors)
    return f"""You are generating a short CRM deal risk explanation for a sales representative.

The risk score, risk level, and risk factors have already been calculated by deterministic rules.
You must not change the score, risk level, or risk factors.
Only explain the existing result in clear business language.

Rules:
- Use only the evidence provided.
- Do not invent facts.
- Do not mention that you are an AI.
- Keep the summary to 2-3 sentences.
- Be direct and useful for a sales rep.
- Customize the explanation for this specific deal; mention the opportunity name when provided.
- If evidence is limited, say the risk is based on the available CRM signals.

Input:
Opportunity name: {_safe_text(opportunity_name)}
Risk score: {risk_score}
Risk level: {risk_level}
Risk factors: {json.dumps(compact_factors, default=str)}
Existing deterministic summary: {_truncate(deterministic_summary)}
Profile narrative: {_safe_text(profile_narrative)}
Recent activity summary: {_safe_text(recent_activity_summary)}

Return only the final reasoning summary."""


def _compact_risk_factors(risk_factors: list) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for risk_factor in risk_factors[:_MAX_FACTOR_COUNT]:
        raw_factor = _to_dict(risk_factor)
        compact.append(
            {
                "factor_type": _truncate(raw_factor.get("factor_type")),
                "description": _truncate(raw_factor.get("description")),
                "severity": _truncate(raw_factor.get("severity")),
                "evidence": _truncate(raw_factor.get("evidence")),
                "source": _truncate(raw_factor.get("source")),
                "confidence": raw_factor.get("confidence"),
            }
        )
    return compact


def _to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if is_dataclass(value):
        return asdict(value)
    return {
        "factor_type": getattr(value, "factor_type", None),
        "description": getattr(value, "description", None),
        "severity": getattr(value, "severity", None),
        "evidence": getattr(value, "evidence", None),
        "source": getattr(value, "source", None),
        "confidence": getattr(value, "confidence", None),
    }


def _safe_text(value: Any) -> str:
    text = _truncate(value)
    return text if text else "Not provided"


def _truncate(value: Any) -> str:
    text = str(value or "").strip()
    return text[:_MAX_TEXT_CHARS]
