"""WritePolicy — invisible middleware that gates every CRM write.

This is **not** a set of tools the LLM calls.  It is structural enforcement
embedded inside ``execute_tool``: when the agent tries to run a write tool,
the policy automatically:

    1. Looks up the action's tier (1/2/3).
    2. Tier 1/2 → executes transparently.
    3. Tier 3 → blocks execution, returns a **confirmation token**.
       The write only executes when the token is sent back
       (i.e. the user clicked "Confirm" in the UI).

The LLM never knows this middleware exists.  It calls ``execute_tool`` and
either gets a result (tier 1/2) or a ``CONFIRMATION_REQUIRED`` response
with a token it must pass back.

Scope note
~~~~~~~~~~
This policy only owns the writer's own structural write-safety: tiering and
confirmation tokens.  Cross-worker concerns — duplicate detection, data-conflict
resolution, capturing ``old_value`` for diffs/corrections, and session memory —
belong to the **orchestrator** (not built yet), which coordinates reader/writer
workers and the session state between them.  They are deliberately absent here.

Confirmation token flow
~~~~~~~~~~~~~~~~~~~~~~~
::

    Agent → execute_tool("delete_person", {...})
    ← CONFIRMATION_REQUIRED + token "abc123" + draft
    Agent → tells user "I need your confirmation to delete …"
    User → clicks Confirm button (UI sends token)
    Agent → execute_tool("delete_person", {...}, confirmation_token="abc123")
    ← executes, returns result

Tokens are single-use and scoped to the session.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from agent.stubs.safety_tools import _lookup_action_tier


@dataclass
class WriteDecision:
    """Result of the write-policy evaluation."""

    allowed: bool
    tier: int
    action: str
    tool_args: dict[str, Any]

    # Set for tier 3 — the confirmation token the user must send back.
    confirmation_token: str | None = None
    # Human-readable reason when ``allowed`` is ``False``.
    reason: str | None = None


@dataclass
class _PendingConfirmation:
    """A tier-3 action awaiting user confirmation."""

    action: str
    tool_args: dict[str, Any]
    created_at: float
    # Expire after 10 minutes.
    ttl: float = 600.0

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl


class WritePolicy:
    """Invisible write-gate middleware.

    Instantiated per session.  Called by the scoped ``execute_tool`` closure
    in ``crm_tools.py`` — the LLM never sees or calls this directly.

    Parameters
    ----------
    session_id:
        Opaque session identifier for duplicate detection and write logging.
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        # token → PendingConfirmation (single-use, popped on confirm).
        self._pending: dict[str, _PendingConfirmation] = {}

    async def gate(
        self,
        action: str,
        tool_args: dict[str, Any],
        confirmation_token: str | None = None,
    ) -> WriteDecision:
        """Evaluate whether the write should proceed.

        Returns a ``WriteDecision``.  The caller (``execute_tool``) checks:
        - ``decision.allowed`` → forward to the bridge.
        - ``not decision.allowed`` → return the decision as an error envelope
          to the LLM (which then surfaces the message / token to the user).
        """
        # ── Fast path: confirmation token provided ──────────────────────
        if confirmation_token is not None:
            return self._validate_token(confirmation_token, action, tool_args)

        # ── 1. Tier lookup ──────────────────────────────────────────────
        tier_result = await _lookup_action_tier(action)
        tier_data = _extract_data(tier_result)
        tier = tier_data.get("tier", 3) if tier_data else 3

        # ── 2. Tier 3 → generate confirmation token ────────────────────
        if tier >= 3:
            token = str(uuid.uuid4())
            self._pending[token] = _PendingConfirmation(
                action=action,
                tool_args=tool_args,
                created_at=time.time(),
            )
            return WriteDecision(
                allowed=False,
                tier=tier,
                action=action,
                tool_args=tool_args,
                confirmation_token=token,
                reason=(
                    "This action requires explicit user confirmation. "
                    "Present the draft to the user and pass back the "
                    "confirmation_token once they approve."
                ),
            )

        # ── 3. Tier 1/2 — allowed ──────────────────────────────────────
        return WriteDecision(
            allowed=True,
            tier=tier,
            action=action,
            tool_args=tool_args,
        )

    def _validate_token(
        self,
        token: str,
        action: str,
        tool_args: dict[str, Any],
    ) -> WriteDecision:
        """Validate and consume a confirmation token."""
        pending = self._pending.pop(token, None)

        if pending is None:
            return WriteDecision(
                allowed=False,
                tier=3,
                action=action,
                tool_args=tool_args,
                reason="Invalid or already-used confirmation token.",
            )

        if pending.expired:
            return WriteDecision(
                allowed=False,
                tier=3,
                action=action,
                tool_args=tool_args,
                reason="Confirmation token has expired. Please retry.",
            )

        if pending.action != action:
            return WriteDecision(
                allowed=False,
                tier=3,
                action=action,
                tool_args=tool_args,
                reason=(
                    f"Token was issued for '{pending.action}', "
                    f"not '{action}'."
                ),
            )

        # Token is valid — allow execution.
        return WriteDecision(
            allowed=True,
            tier=3,
            action=action,
            tool_args=tool_args,
        )

    def clear_pending(self) -> None:
        """Discard all pending confirmation tokens (useful in tests)."""
        self._pending.clear()


def _extract_data(result: dict) -> dict[str, Any] | None:
    """Extract the ``data`` payload from a bridge-style envelope."""
    if result.get("ok") and "data" in result:
        return result["data"]
    return None
