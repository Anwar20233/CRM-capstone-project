from fastapi import APIRouter

from followup.api.routes_context import router as context_router
from followup.api.routes_risk import router as risk_router

router = APIRouter()
router.include_router(context_router)
router.include_router(risk_router)
