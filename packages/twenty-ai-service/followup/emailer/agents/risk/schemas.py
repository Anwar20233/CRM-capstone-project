from typing import Literal

from pydantic import BaseModel, Field

RiskLevel = Literal["LOW", "MEDIUM", "HIGH"]


class RiskScore(BaseModel):
    level: RiskLevel
    score: int = Field(ge=0, le=100)
    reasoning: str = ""
