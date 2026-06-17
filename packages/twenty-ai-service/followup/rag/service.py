"""Shared RAG retrieval contracts (Person 1 infra).

NOTE: Minimal placeholder defining the `RetrievalService` protocol and
`RetrievedChunk` model consumed by Person 2 (Next Step) and Person 4
(Drafting). Person 1 owns the real implementation (file-based retriever for
Phase 1, pgvector for Phase 2) per
FOLLOWUP_INTELLIGENCE_LAYER_FINAL_SPEC.md §6.6.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from followup.rag.collections import CollectionName


class RetrievedChunk(BaseModel):
    """A single retrieved document chunk with provenance."""

    content: str
    source: str
    collection: CollectionName
    score: float = 0.0
    metadata: dict[str, str] = {}


@runtime_checkable
class RetrievalService(Protocol):
    """Protocol implemented by Person 1's retrieval backends."""

    async def retrieve_documents(
        self,
        query: str,
        collection: CollectionName,
        top_k: int = 5,
    ) -> list[RetrievedChunk]:
        """Return up to `top_k` chunks from `collection` relevant to `query`."""
        ...
