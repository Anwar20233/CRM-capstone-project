from pathlib import Path

from followup.emailer.rag.collections import CollectionName
from followup.emailer.rag.service import RetrievedChunk
from followup.knowledge import skill_store

_KNOWLEDGE_ROOT = Path(__file__).resolve().parent.parent / "knowledge"

_COLLECTION_DIRS: dict[CollectionName, str] = {
    CollectionName.EMAIL_TEMPLATES: "email_templates",
    CollectionName.PROPOSAL_TEMPLATES: "proposal_templates",
    CollectionName.PRODUCT_CATALOG: "product_catalog",
    CollectionName.SERVICE_CATALOG: "service_catalog",
    CollectionName.SALES_PLAYBOOKS: "playbooks",
    CollectionName.BANT: "bant",
    CollectionName.INDUSTRY_EXAMPLES: "industry_examples",
}

# Collections users can teach through the Skills UI: each maps to a skill-name
# prefix so a company's house-style templates and real product/service offerings
# are discovered here (the drafter then writes in that style and grounds
# proposals in them). Other collections stay file-based.
_COLLECTION_SKILL_PREFIXES: dict[CollectionName, str] = {
    CollectionName.EMAIL_TEMPLATES: skill_store.EMAIL_TEMPLATE_PREFIX,
    CollectionName.PROPOSAL_TEMPLATES: skill_store.PROPOSAL_TEMPLATE_PREFIX,
    CollectionName.PRODUCT_CATALOG: skill_store.PRODUCT_CATALOG_PREFIX,
    CollectionName.SERVICE_CATALOG: skill_store.SERVICE_CATALOG_PREFIX,
}

_DRAFT_TYPE_TO_TEMPLATE: dict[str, str] = {
    "follow_up_email": "follow_up.md",
    "meeting_recap_email": "meeting_recap.md",
    "proposal_delivery_email": "follow_up.md",
    "re_engagement_email": "re_engagement.md",
    "reminder_email": "follow_up.md",
    "product_proposal": "product_proposal.md",
    "service_proposal": "service_proposal.md",
    "industry_proposal": "product_proposal.md",
}


def _score_chunk(query: str, content: str, file_name: str) -> float:
    query_terms = {term.lower() for term in query.split() if term.strip()}
    haystack = f"{file_name} {content}".lower()
    if not query_terms:
        return 0.0
    matches = sum(1 for term in query_terms if term in haystack)
    return matches / len(query_terms)


class FileRetriever:
    """Phase 1 keyword/file retriever reading markdown from followup/knowledge/."""

    def __init__(self, knowledge_root: Path | None = None) -> None:
        self._knowledge_root = knowledge_root or _KNOWLEDGE_ROOT

    async def retrieve_documents(
        self,
        query: str,
        collection: CollectionName,
        top_k: int = 3,
    ) -> list[RetrievedChunk]:
        # Company-edited skills win for template collections; bundled markdown
        # is the fallback when none have been provisioned.
        skill_chunks = await self._retrieve_from_skills(query, collection)
        if skill_chunks:
            return self._rank(skill_chunks, top_k)

        return self._rank(self._retrieve_from_files(query, collection), top_k)

    async def _retrieve_from_skills(
        self, query: str, collection: CollectionName
    ) -> list[RetrievedChunk]:
        prefix = _COLLECTION_SKILL_PREFIXES.get(collection)
        if prefix is None:
            return []

        preferred_file = _DRAFT_TYPE_TO_TEMPLATE.get(query.strip().lower())
        chunks: list[RetrievedChunk] = []
        for row in await skill_store.fetch_skills_by_prefix(prefix):
            # Synthesize a file_name from the skill key so the existing
            # preferred-template mapping and scoring apply unchanged.
            file_name = f"{row.key}.md"
            score = _score_chunk(query, row.content, file_name)
            if preferred_file and file_name == preferred_file:
                score = max(score, 1.0)
            chunks.append(
                RetrievedChunk(
                    document_id=row.key,
                    content=row.content,
                    score=score,
                    metadata={
                        "file_name": file_name,
                        "collection": collection.value,
                        "source": "skill",
                        "skill_name": row.name,
                    },
                )
            )
        return chunks

    def _retrieve_from_files(
        self, query: str, collection: CollectionName
    ) -> list[RetrievedChunk]:
        collection_dir_name = _COLLECTION_DIRS.get(collection)
        if collection_dir_name is None:
            return []

        collection_dir = self._knowledge_root / collection_dir_name
        if not collection_dir.is_dir():
            return []

        preferred_file = _DRAFT_TYPE_TO_TEMPLATE.get(query.strip().lower())
        chunks: list[RetrievedChunk] = []

        for file_path in sorted(collection_dir.glob("*.md")):
            content = file_path.read_text(encoding="utf-8")
            score = _score_chunk(query, content, file_path.name)
            if preferred_file and file_path.name == preferred_file:
                score = max(score, 1.0)
            chunks.append(
                RetrievedChunk(
                    document_id=file_path.stem,
                    content=content,
                    score=score,
                    metadata={"file_name": file_path.name, "collection": collection.value},
                )
            )
        return chunks

    @staticmethod
    def _rank(chunks: list[RetrievedChunk], top_k: int) -> list[RetrievedChunk]:
        chunks.sort(key=lambda chunk: chunk.score, reverse=True)
        return chunks[:top_k]
