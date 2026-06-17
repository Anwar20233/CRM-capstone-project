from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from agent.llm_client import LLMClient
from followup.context.crm_fetch import RawOpportunityBundle
from followup.context.errors import LlmExtractError
from followup.context.schemas import DealContext

logger = logging.getLogger(__name__)

_EXTRACT_SYSTEM_PROMPT = (
    "You map raw Twenty CRM JSON into a DealContext object for follow-up agents. "
    "Preserve UUIDs exactly. Use ISO-8601 datetimes with timezone. "
    "Do not invent records or fields. "
    "Convert amountMicros to a float amount in the main currency unit. "
    "Map stage labels to canonical uppercase snake-case values such as PROPOSAL. "
    "Only set stage_entered_at when the CRM bundle includes stageEnteredAt. "
    "Summarize note bodies into timeline.summary when helpful. "
    "Include opportunity scalar fields notes and emailText as timeline entries when present. "
    "Return JSON only."
)

_EXTRACT_USER_TEMPLATE = """Map this CRM bundle into DealContext JSON.

Required top-level keys:
- opportunity: {{id, name, stage, amount, close_date, company_id, owner_id, updated_at, stage_entered_at}}
- company: {{id, name, industry}} or null
- contacts: [{{id, name, role, is_decision_maker}}]
- timeline: [{{type, title, summary, occurred_at}}]
- tasks: [{{id, title, status, due_at, is_overdue}}]
- meetings: [{{id, title, starts_at, status}}]
- pipeline_meta: {{stages: [string], stage_sla_days: {{stage: days}}, source}}

Do not include engagement, loaded_at, or context_provenance.

CRM bundle:
{bundle_json}
"""


def _bundle_to_prompt_payload(bundle: RawOpportunityBundle) -> dict[str, Any]:
    return bundle.model_dump(mode="json")


async def extract_deal_context(
    bundle: RawOpportunityBundle,
    *,
    llm_client: LLMClient | None = None,
) -> DealContext:
    client = llm_client or LLMClient()
    prompt = _EXTRACT_USER_TEMPLATE.format(
        bundle_json=json.dumps(_bundle_to_prompt_payload(bundle), default=str),
    )

    try:
        response = client.get_openai_client().chat.completions.create(
            model=client.model,
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        parsed = json.loads(content)
        context = DealContext.model_validate(parsed)
        return context.model_copy(update={"context_provenance": "hybrid"})
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
        logger.warning("LLM deal context extraction failed: %s", error)
        raise LlmExtractError(str(error)) from error
    except Exception as error:
        logger.warning("LLM deal context extraction request failed: %s", error)
        raise LlmExtractError(str(error)) from error
