from __future__ import annotations

import os
import sys


def require_sweep_env() -> tuple[str, str, str]:
    workspace_id = os.environ.get("TWENTY_WORKSPACE_ID", "")
    user_id = os.environ.get("TWENTY_USER_ID", "")
    role_id = (
        os.environ.get("TWENTY_READER_ROLE_ID")
        or os.environ.get("TWENTY_ROLE_ID")
        or ""
    )

    if not workspace_id:
        print("TWENTY_WORKSPACE_ID is required", file=sys.stderr)
        raise SystemExit(1)
    if not user_id:
        print(
            "TWENTY_USER_ID is required for authenticated sweep context loading",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if not role_id:
        print(
            "TWENTY_READER_ROLE_ID or TWENTY_ROLE_ID is required",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return workspace_id, user_id, role_id
