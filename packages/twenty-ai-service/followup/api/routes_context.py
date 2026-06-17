from fastapi import APIRouter, HTTPException, Query

from followup.context.errors import ContextLoadError
from followup.context.loader import load_deal_context
from followup.context.schemas import DealContext

router = APIRouter(prefix="/followup", tags=["followup"])


@router.get("/context/{opportunity_id}", response_model=DealContext)
async def get_deal_context(
    opportunity_id: str,
    workspace_id: str = Query(...),
    user_id: str = Query(...),
    role_id: str | None = Query(default=None),
    use_llm: bool = Query(default=True),
) -> DealContext:
    try:
        return await load_deal_context(
            opportunity_id,
            workspace_id,
            user_id,
            role_id=role_id,
            use_llm=use_llm,
        )
    except ContextLoadError as error:
        status_code = 404 if error.code == "OPPORTUNITY_NOT_FOUND" else 502
        raise HTTPException(
            status_code=status_code,
            detail={"code": error.code, "message": error.message},
        ) from error
