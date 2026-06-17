from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CrmIdentity:
    workspace_id: str
    user_id: str
    role_id: str


def resolve_crm_identity(
    workspace_id: str,
    user_id: str,
    *,
    role_id: str | None = None,
) -> CrmIdentity:
    resolved_role_id = (
        role_id
        or os.environ.get("TWENTY_READER_ROLE_ID")
        or os.environ.get("TWENTY_ROLE_ID")
    )
    if not resolved_role_id:
        raise RuntimeError(
            "Neither role_id argument nor TWENTY_READER_ROLE_ID / TWENTY_ROLE_ID is set",
        )
    return CrmIdentity(
        workspace_id=workspace_id,
        user_id=user_id,
        role_id=resolved_role_id,
    )
