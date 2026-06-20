"""Tests for manual step edits — ``apply_step_edits`` and the projection it feeds.

The rep can hand-edit a pending action's steps (email subject/body, note body,
task title, meeting title, stage intent) before accepting. These edits must land
in the exact payload locations the executor reads on accept, and the re-projected
steps must reflect them so the card shows the saved content. Pure data — no DB.
"""

from __future__ import annotations

import uuid

import pytest

from followup.api.models import (
    PendingActionResponse,
    StepEdit,
    apply_step_edits,
)
from followup.store.repositories import PendingAction


def _action() -> PendingAction:
    return PendingAction(
        id=uuid.uuid4(),
        opportunity_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        trigger_type="email",
        action_type="multi_step",
        action_payload={
            "steps": [
                {"kind": "draft_email", "intent": "reach out", "metadata": {"title": "Email"}},
                {"kind": "write_note", "intent": "log it", "metadata": {"title": "Note"}},
                {"kind": "create_task", "intent": "do it", "metadata": {"title": "Task"}},
                {"kind": "update_stage", "intent": "advance", "metadata": {"title": "Stage"}},
            ],
            "draft": {"subject": "Old", "body": "Old body", "recipient_email": "a@b.com"},
            "task_results": {
                "write_note": {"body": "old note"},
                "create_task": {"title": "old task"},
            },
        },
        draft_result={"subject": "Old", "body": "Old body"},
    )


def test_edits_land_in_executor_payload_locations() -> None:
    action = _action()

    apply_step_edits(
        action,
        [
            StepEdit(index=0, email_subject="New subject", email_body="New body"),
            StepEdit(index=1, detail="new note body"),
            StepEdit(index=2, title="new task title"),
            StepEdit(index=3, detail="Move to Closed Won"),
        ],
    )

    payload = action.action_payload
    # draft_email writes both the payload draft and draft_result (readers differ).
    assert payload["draft"]["subject"] == "New subject"
    assert payload["draft"]["body"] == "New body"
    assert action.draft_result["subject"] == "New subject"
    assert action.draft_result["body"] == "New body"
    # note body and task title go to the prep-task artifacts the executor reads.
    assert payload["task_results"]["write_note"]["body"] == "new note body"
    assert payload["task_results"]["create_task"]["title"] == "new task title"
    # task title also updates the step label so the card stays consistent.
    assert payload["steps"][2]["metadata"]["title"] == "new task title"
    # update_stage edits the step intent the writer reads to pick the stage.
    assert payload["steps"][3]["intent"] == "Move to Closed Won"


def test_projection_reflects_saved_edits() -> None:
    action = _action()

    apply_step_edits(
        action,
        [
            StepEdit(index=0, email_subject="New subject"),
            StepEdit(index=1, detail="new note body"),
            StepEdit(index=2, title="new task title"),
            StepEdit(index=3, detail="Move to Closed Won"),
        ],
    )

    steps = PendingActionResponse._build_steps(action)
    assert steps[0].email_subject == "New subject"
    assert steps[1].detail == "new note body"
    assert steps[2].title == "new task title"
    assert steps[3].detail == "Move to Closed Won"


def test_unknown_index_is_ignored() -> None:
    action = _action()
    # Should not raise and should leave the payload untouched.
    apply_step_edits(action, [StepEdit(index=99, detail="nope")])
    assert action.action_payload["task_results"]["write_note"]["body"] == "old note"


def test_none_fields_leave_content_unchanged() -> None:
    action = _action()
    apply_step_edits(action, [StepEdit(index=0)])  # all fields None
    assert action.action_payload["draft"]["subject"] == "Old"
    assert action.action_payload["draft"]["body"] == "Old body"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
