"""Inbound trigger contracts for the Follow-Up pipeline.

EmailSignalEvent is the raw inbound signal — an email arriving that the
orchestrator converts into a NextStepRequest (trigger_type="email_signal").
It is NOT an agent call; it's the boundary event that starts the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SIGNAL_TYPES: frozenset[str] = frozenset({"email_signal", "manual", "scheduled"})


@dataclass
class EmailSignalEvent:
    """An inbound email that triggers the follow-up pipeline.

    ``signal_type`` is always "email_signal" for this event class; the
    frozenset exists so the orchestrator can validate trigger_type at runtime.
    ``received_at`` is an ISO-8601 string so asdict() is JSON-safe.
    """

    sender_email: str
    subject: str
    body: str
    received_at: str  # ISO-8601
    opportunity_id: str | None = None
    signal_type: str = field(default="email_signal")


__all__ = ["EmailSignalEvent", "SIGNAL_TYPES"]
