"""NER route — the first capability of the global AI service.

Future workflow phases mount additional routers (e.g. /agent) on the same app
and port; this module stays focused on entity extraction.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from pipelines import extract

router = APIRouter(prefix="/ner", tags=["ner"])


class ExtractRequest(BaseModel):
    text: str = Field(..., min_length=1)


class Entity(BaseModel):
    label: str
    text: str
    score: float
    start: int | None = None
    end: int | None = None


class ExtractResponse(BaseModel):
    entities: list[Entity]


@router.post("/extract", response_model=ExtractResponse)
def extract_entities(request: ExtractRequest) -> ExtractResponse:
    entities = extract(request.text)
    return ExtractResponse(entities=entities)
