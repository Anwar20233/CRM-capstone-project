"""P4 — Draft Engine contracts: request/result types + agent interface.

The Draft Engine writes a ready-to-send email from the *content + context* the
follow-up agent hands it — the draft ``intent`` (from the plan step), the email
``classification``, the deal picture, and the inbound email being replied to.
The drafter owns its own templates and rules: notably the **tone** is the
drafter's decision (derived from the classification), NOT injected by the
follow-up agent. ``MockDraftingAgent`` is the stand-in; it picks a tone and
renders a body from those inputs so every field is populated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from followup.profile.schemas import DealContext

DRAFT_TONE_TYPES: frozenset[str] = frozenset(
    {"professional", "casual", "urgent", "consultative"}
)

DRAFT_MODES: frozenset[str] = frozenset({"single", "sweep"})


@dataclass
class DraftRequest:
    """Input bundle for the Draft Engine agent — content + context, no tone.

    ``intent`` is the plan step's high-level goal ("reassure on the timeline").
    ``classification`` is the email triage result; the drafter reads it to pick
    its own tone. The follow-up agent sends context, not styling decisions.
    """

    deal_context: DealContext
    intent: str  # the draft step's high-level goal (what to say)
    classification: dict[str, Any] | None = None  # the drafter derives tone from this
    mode: str = "single"  # ∈ DRAFT_MODES
    recipient_email: str | None = None
    previous_draft: str | None = None
    # Step 5 (calendar): asdict'd TimeSlots to offer when the step is book_meeting.
    available_slots: list[dict] | None = None
    # The inbound email being replied to (email trigger path). Lets the draft
    # engine address the sender and reference the specific points they raised.
    reply_context: dict | None = None  # {sender_email, sender_name, subject, body}
    # The rep's IANA timezone, so proposed meeting times are rendered in local
    # wall-clock ("1:00 PM (Asia/Riyadh)") instead of raw UTC in the email.
    timezone: str | None = None


@dataclass
class DraftResult:
    """Output of the Draft Engine agent.

    All fields are str/list/dict/None so asdict(result) is JSON-safe for
    storage in PendingAction.draft_result.
    """

    opportunity_id: str
    subject: str
    body: str
    recipient_email: str | None
    tone: str
    drafted_at: str  # ISO-8601
    metadata: dict = field(default_factory=dict)


@runtime_checkable
class DraftingAgent(Protocol):
    async def run(self, request: DraftRequest) -> DraftResult: ...


class MockDraftingAgent:
    """Stand-in drafting agent; builds a realistic draft from intent + context.

    It chooses the tone itself (its own rule) from the classification, then
    renders a body around the ``intent``. When ``reply_context`` is present the
    draft is framed as a reply to the inbound email.
    """

    _FALLBACK_RECIPIENT = "sales-team@example.com"

    # The drafter's own tone rule — derived from the classification it is given.
    def _choose_tone(self, classification: dict[str, Any] | None) -> str:
        classification = classification or {}
        urgency = (classification.get("urgency") or "").lower()
        email_type = (classification.get("type") or "").lower()
        if urgency == "high" or email_type == "objection":
            return "urgent"
        if email_type in ("buying_signal", "meeting_request"):
            return "consultative"
        if email_type == "info_sharing":
            return "casual"
        return "professional"

    async def run(self, request: DraftRequest) -> DraftResult:
        ctx = request.deal_context
        reply = request.reply_context
        tone = self._choose_tone(request.classification)

        # Recipient: prefer the inbound sender, then explicit, then first contact.
        if reply and reply.get("sender_email"):
            recipient = reply["sender_email"]
            contact_name = reply.get("sender_name") or recipient.split("@")[0].replace(".", " ").title()
        elif request.recipient_email:
            recipient = request.recipient_email
            contact_name = next(
                (c.name for c in ctx.contacts if c.email == recipient and c.name),
                recipient.split("@")[0].replace(".", " ").title(),
            )
        elif ctx.contacts and ctx.contacts[0].email:
            recipient = ctx.contacts[0].email
            contact_name = ctx.contacts[0].name or "there"
        else:
            recipient = self._FALLBACK_RECIPIENT
            contact_name = "there"

        # Subject: reply thread when replying, otherwise a fresh subject.
        if reply and reply.get("subject"):
            subject = f"Re: {reply['subject']}"
        else:
            subject = f"Re: {ctx.opportunity_name} — follow-up"

        tone_opener = {
            "professional": "Thank you for your message.",
            "casual": "Thanks for getting back to us!",
            "urgent": "Thank you for flagging this — I want to make sure we address your concerns promptly.",
            "consultative": "I appreciate you sharing these details.",
        }.get(tone, "Thank you for your message.")

        lines: list[str] = [f"Hi {contact_name},", "", tone_opener, ""]

        if reply and reply.get("body"):
            lines.append(
                f"Regarding {ctx.opportunity_name}, I've reviewed your points "
                f"and wanted to follow up on the key items you raised."
            )
            lines.append("")

        # The plan step's intent is the core message the drafter expands.
        if request.intent:
            lines.append(request.intent)
            lines.append("")

        if request.available_slots:
            available = [s for s in request.available_slots if s.get("available")]
            if available:
                lines.append("I have the following times available:")
                for slot in available[:3]:
                    lines.append(f"  • {slot.get('start', '?')} – {slot.get('end', '?')}")
                lines.append("")
                lines.append("Please let me know which works best for you.")
                lines.append("")

        lines.append("Best regards,")
        lines.append("[Your Name]")
        lines.append("BeamData")

        return DraftResult(
            opportunity_id=ctx.opportunity_id,
            subject=subject,
            body="\n".join(lines),
            recipient_email=recipient,
            tone=tone,
            drafted_at=datetime.now(timezone.utc).isoformat(),
            metadata={
                "intent": request.intent,
                "mode": request.mode,
                "contacts_count": len(ctx.contacts),
                "is_reply": reply is not None,
            },
        )


async def run_draft(
    request: DraftRequest, *, agent: DraftingAgent | None = None
) -> DraftResult:
    return await (agent or MockDraftingAgent()).run(request)


async def mock_run_draft(request: DraftRequest) -> DraftResult:
    return await MockDraftingAgent().run(request)


__all__ = [
    "DraftRequest",
    "DraftResult",
    "DraftingAgent",
    "MockDraftingAgent",
    "DRAFT_TONE_TYPES",
    "DRAFT_MODES",
    "run_draft",
    "mock_run_draft",
]
