"""Bridge router — the semantic tool layer between the agent and Twenty.

The orchestrator sends { tool, args } style requests here; this router maps the
Python snake_case contract to the Node bridge's camelCase contract and forwards
over localhost HTTP. The Node bridge is intentionally unguarded, so any auth /
authorization decisions belong upstream of this router.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from bridge_client import forward as _forward

router = APIRouter(prefix="/bridge", tags=["bridge"])


class ExecuteRequest(BaseModel):
    tool: str = Field(..., min_length=1)
    args: dict | None = None
    workspace_id: str = Field(..., min_length=1)
    role_id: str = Field(..., min_length=1)
    # Required for DATABASE_CRUD tools. user_workspace_id is auto-resolved from
    # user_id + workspace_id by the Node bridge when omitted.
    user_id: str | None = None
    user_workspace_id: str | None = None


class CatalogRequest(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    role_id: str = Field(..., min_length=1)
    categories: list[str] | None = None


class LearnRequest(BaseModel):
    tool_names: list[str] = Field(..., min_length=1)
    workspace_id: str = Field(..., min_length=1)
    role_id: str = Field(..., min_length=1)


class CurrentUserRequest(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)


@router.post("/execute")
async def execute(request: ExecuteRequest) -> dict:
    payload: dict = {
        "tool": request.tool,
        "args": request.args or {},
        "workspaceId": request.workspace_id,
        "roleId": request.role_id,
    }

    if request.user_id is not None:
        payload["userId"] = request.user_id

    if request.user_workspace_id is not None:
        payload["userWorkspaceId"] = request.user_workspace_id

    return await _forward("execute", payload)


@router.post("/catalog")
async def catalog(request: CatalogRequest) -> dict:
    payload: dict = {
        "workspaceId": request.workspace_id,
        "roleId": request.role_id,
    }

    if request.categories is not None:
        payload["categories"] = request.categories

    return await _forward("catalog", payload)


@router.post("/learn")
async def learn(request: LearnRequest) -> dict:
    return await _forward(
        "learn",
        {
            "toolNames": request.tool_names,
            "workspaceId": request.workspace_id,
            "roleId": request.role_id,
        },
    )


@router.post("/current-user")
async def current_user(request: CurrentUserRequest) -> dict:
    return await _forward(
        "current-user",
        {
            "workspaceId": request.workspace_id,
            "userId": request.user_id,
        },
    )
