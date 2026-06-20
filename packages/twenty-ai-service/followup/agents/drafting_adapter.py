"""Adapter: the real Draft Engine behind the orchestrator's ``DraftingAgent``
contract.

The orchestrator speaks ``DraftRequest -> DraftResult`` (one finished email).
The merged drafter speaks ``DealContext + FollowUpEvent + draft_types +
retrieval -> DraftingAgentResult`` (lists of email/proposal drafts, RAG-backed).
This adapter:

* IN  — translate the orchestrator's deal picture + reply context, pick the
  single email ``DraftType`` the situation calls for, and inject a shared
  file-backed retriever. No database reads.
* OUT — collapse the agent's first email draft into the orchestrator's
  ``DraftResult`` (recipient/tone filled from the request + classification).

If the agent produces nothing usable, it falls back to the deterministic mock so
the orchestrator always gets a draft to bundle for rep review.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

from followup.agents.mapping import to_drafting_context
from followup.contracts.drafting import DraftRequest, DraftResult, MockDraftingAgent
from followup.emailer.agents.drafting.agent import run_drafting_agent
from followup.emailer.agents.drafting.schemas import DraftType, EmailDraft
from followup.emailer.events.schemas import FollowUpEvent
from followup.emailer.rag.file_retriever import FileRetriever
from followup.emailer.rag.service import RetrievalService
from tracing import traceable

logger = logging.getLogger(__name__)

_UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_INTERNAL_ID_PHRASE = re.compile(
    r"\(?\s*ID:\s*\)?",
    re.IGNORECASE,
)
_PLACEHOLDER_LINE = re.compile(
    r"^\s*\[(?:Your Name|Your Position|Your Company|Your Contact Information)\]\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def sanitize_draft_text(text: str) -> str:
    """Strip internal CRM identifiers the drafter must never expose to recipients."""
    cleaned = _UUID_PATTERN.sub("", text)
    cleaned = _INTERNAL_ID_PHRASE.sub("", cleaned)
    cleaned = re.sub(r"\bopportunity\s*,", "deal", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"opportunity\s*\(\s*\)", "deal", cleaned, flags=re.IGNORECASE)
    cleaned = _PLACEHOLDER_LINE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()

# The orchestrator bundles ONE email per draft step, so we always ask the drafter
# for a single, explicit email type chosen from the email classification. Most
# inbound types are an active follow-up (address the point + advance the deal);
# only a genuinely cold deal is a re-engagement, and a buying signal warrants the
# proposal-delivery framing. (meeting_recap is reserved for meeting_completed.)
_CLASSIFICATION_TO_DRAFT_TYPE: dict[str, DraftType] = {
    "re_engagement": DraftType.RE_ENGAGEMENT_EMAIL,
    "buying_signal": DraftType.PROPOSAL_DELIVERY_EMAIL,
}

# Tone the orchestrator records on the DraftResult (the drafter owns prose, not
# the orchestrator's tone vocabulary, so we map the classification here).
_CLASSIFICATION_TO_TONE: dict[str, str] = {
    "objection": "urgent",
    "buying_signal": "consultative",
    "meeting_request": "consultative",
    "info_sharing": "casual",
}


def _draft_type_for(classification: dict | None) -> DraftType:
    email_type = ((classification or {}).get("type") or "").lower()
    return _CLASSIFICATION_TO_DRAFT_TYPE.get(email_type, DraftType.FOLLOW_UP_EMAIL)


def _tone_for(classification: dict | None) -> str:
    classification = classification or {}
    if (classification.get("urgency") or "").lower() == "high":
        return "urgent"
    return _CLASSIFICATION_TO_TONE.get((classification.get("type") or "").lower(), "professional")


def _slot_lines(available_slots: list[dict] | None) -> str:
    if not available_slots:
        return ""
    offered = [s for s in available_slots if s.get("available")][:3]
    if not offered:
        return ""
    bullets = "\n".join(f"  - {s.get('start', '?')} to {s.get('end', '?')}" for s in offered)
    return f"\n\nProposed meeting times to offer:\n{bullets}"


class OrchestratorDraftingAgent:
    """Real drafter wrapped to satisfy the orchestrator's ``DraftingAgent``.

    The file retriever is built once and shared across runs (it reads static
    markdown templates from ``followup/emailer/knowledge/``).
    """

    def __init__(
        self, model: str | None = None, retrieval: RetrievalService | None = None
    ) -> None:
        self._model = model
        self._retrieval = retrieval or FileRetriever()
        self._mock = MockDraftingAgent()

    @traceable(name="drafting_agent", run_type="chain")
    async def run(self, request: DraftRequest) -> DraftResult:
        try:
            recipient_email = request.recipient_email
            if request.reply_context and request.reply_context.get("sender_email"):
                recipient_email = request.reply_context["sender_email"]
            context = to_drafting_context(
                request.deal_context, recipient_email=recipient_email
            )
            # Thread the orchestrator's directive (the "why + what + manner" the
            # next-step agent chose) and any calendar slots into the deal's notes
            # so the drafter's prompt sees them as concrete guidance.
            context = self._with_directive(context, request)
            result = await run_drafting_agent(
                context=context,
                event=self._synthetic_event(request),
                draft_types=[_draft_type_for(request.classification)],
                retrieval=self._retrieval,
                model=self._model,
            )
        except Exception as error:  # noqa: BLE001 — never crash the orchestrator
            logger.exception("drafting adapter failed; falling back to mock: %s", error)
            return await self._mock.run(request)

        email = result.email_drafts[0] if result.email_drafts else None
        if email is None:
            return await self._mock.run(request)
        return self._to_draft_result(email, request)

    def _with_directive(self, context, request: DraftRequest):
        """Inject the directive + slots as a leading note the drafter prompt reads."""
        from followup.emailer.context.schemas import NoteSummary

        directive = request.intent or ""
        reply = request.reply_context or {}
        if reply.get("sender_email"):
            sender_label = reply.get("sender_name") or reply["sender_email"]
            directive = (
                f"Recipient: {sender_label} ({reply['sender_email']}).\n"
                f"{directive}"
            )
        slots = _slot_lines(request.available_slots)
        if reply.get("body"):
            directive += f"\n\nInbound message to answer:\n{reply['body']}"
        directive += slots
        if not directive.strip():
            return context
        note = NoteSummary(
            id="orchestrator-directive",
            title="Follow-up directive",
            body=directive.strip(),
            created_at=datetime.now(timezone.utc),
        )
        return context.model_copy(update={"recent_notes": [note, *context.recent_notes]})

    @staticmethod
    def _synthetic_event(request: DraftRequest) -> FollowUpEvent:
        # The drafter only reads context/draft_types; the event is metadata the
        # orchestrator does not source from its profile context.
        return FollowUpEvent(
            event_id=str(uuid.uuid4()),
            idempotency_key=str(uuid.uuid4()),
            event_type="activity_logged",
            opportunity_id=str(request.deal_context.opportunity_id),
            workspace_id="orchestrator",
            user_id="orchestrator",
            occurred_at=datetime.now(timezone.utc),
        )

    def _to_draft_result(self, email: EmailDraft, request: DraftRequest) -> DraftResult:
        ctx = request.deal_context
        recipient = request.recipient_email or next(
            (c.email for c in ctx.contacts if c.email), None
        )
        if request.reply_context and request.reply_context.get("sender_email"):
            recipient = request.reply_context["sender_email"]
        return DraftResult(
            opportunity_id=str(ctx.opportunity_id),
            subject=sanitize_draft_text(email.subject),
            body=sanitize_draft_text(email.body),
            recipient_email=recipient,
            tone=_tone_for(request.classification),
            drafted_at=datetime.now(timezone.utc).isoformat(),
            metadata={
                "intent": request.intent,
                "draft_type": email.draft_type.value,
                "template_used": email.template_used,
                "quality_score": email.quality_score,
                "source": "drafting_agent",
            },
        )


__all__ = ["OrchestratorDraftingAgent", "sanitize_draft_text"]
