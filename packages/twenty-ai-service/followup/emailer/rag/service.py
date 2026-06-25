from typing import Protocol

from pydantic import BaseModel, Field

from followup.emailer.rag.collections import CollectionName


class RetrievedChunk(BaseModel):
    document_id: str
    content: str
    score: float = 1.0
    metadata: dict[str, str] = Field(default_factory=dict)


class RetrievalService(Protocol):
    async def retrieve_documents(
        self,
        query: str,
        collection: CollectionName,
        top_k: int = 3,
    ) -> list[RetrievedChunk]:
        ...
