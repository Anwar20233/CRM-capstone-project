"""Browse + edit the Follow-Up agents' knowledge files from the Skills UI.

Unlike the DB-backed *skills* (``core.skill``), these endpoints expose the real
markdown knowledge on disk under each agent's ``knowledge/`` folder — the
playbooks, frameworks, templates, and catalogs the agents read — so a user can
edit them directly from the product UI. Agent *code* is intentionally out of
scope: only the knowledge directories are reachable.

Scope + safety:
  * Only each agent's ``knowledge/`` directory is reachable. Every request path
    is resolved and re-checked to be inside an allowed root (path-traversal guard).
  * Only text knowledge files are listed/served; ``__pycache__`` / dotfiles are
    skipped.

Knowledge edits apply on the agent's next run — no service restart required.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/followup/agent-files", tags=["followup-agent-files"])

# followup/ package root → each agent's knowledge directory (content only,
# never code). These are the only directories the UI can browse or write.
_FOLLOWUP_ROOT = Path(__file__).resolve().parent.parent
_AGENT_ROOTS: dict[str, Path] = {
    "emailer": _FOLLOWUP_ROOT / "emailer" / "knowledge",
    "next_step": _FOLLOWUP_ROOT / "next_step" / "knowledge",
}

# Text knowledge files a user can edit. Anything else is hidden so the UI never
# serves or overwrites a binary.
_EDITABLE_SUFFIXES = {
    ".md",
    ".json",
    ".txt",
    ".yaml",
    ".yml",
}

_MAX_WRITE_BYTES = 1_000_000  # reject absurd payloads


class AgentFile(BaseModel):
    # ``path`` is the opaque handle used to read/save — never shown in the UI.
    path: str
    agent: str
    folder: str  # immediate parent folder key, used to group into sections
    title: str  # human-friendly name derived from the filename
    category: str  # human-friendly kind derived from the folder (e.g. "Playbook")
    preview: str  # first meaningful line of content, for the card subtitle


# Folder name -> the friendly "kind" shown as a tag, mirroring how the
# user-authored skills are labelled in the same tabs.
_CATEGORY_LABELS: dict[str, str] = {
    "playbooks": "Playbook",
    "email_templates": "Email template",
    "proposal_templates": "Proposal template",
    "product_catalog": "Product catalog",
    "service_catalog": "Service catalog",
    "knowledge": "Framework",
}

# Acronyms that should not be title-cased away.
_ACRONYMS = {"bant": "BANT", "saas": "SaaS", "crm": "CRM"}


def _humanize(stem: str) -> str:
    words = stem.replace("-", " ").replace("_", " ").split()
    return " ".join(_ACRONYMS.get(word.lower(), word.capitalize()) for word in words)


def _category_for(relative: Path) -> str:
    folder = relative.parent.name
    return _CATEGORY_LABELS.get(folder, _humanize(folder))


def _preview_of(path: Path) -> str:
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.lstrip("#> -*").strip()
            if line:
                return line[:140]
    except (OSError, UnicodeDecodeError):
        pass
    return ""


class AgentFileContent(BaseModel):
    path: str
    content: str


class SaveAgentFileRequest(BaseModel):
    path: str
    content: str


def _is_hidden_or_cache(path: Path) -> bool:
    return any(part == "__pycache__" or part.startswith(".") for part in path.parts)


def _resolve_within_roots(rel_path: str) -> Path:
    """Resolve a client-supplied relative path, guarding against traversal.

    Raises 400 for a bad/escaping path and 404 when the file is outside the
    allowed roots or not an editable text file.
    """
    if not rel_path or rel_path.startswith("/"):
        raise HTTPException(status_code=400, detail="A relative file path is required")

    candidate = (_FOLLOWUP_ROOT / rel_path).resolve()

    inside_root = any(
        candidate == root or root in candidate.parents
        for root in _AGENT_ROOTS.values()
    )
    if not inside_root:
        raise HTTPException(status_code=404, detail="File is not within an agent directory")
    if candidate.suffix not in _EDITABLE_SUFFIXES:
        raise HTTPException(status_code=404, detail="File type is not editable")
    return candidate


@router.get("", response_model=list[AgentFile])
def list_agent_files() -> list[AgentFile]:
    """List each agent's knowledge files as skill-like cards (title/kind/preview)."""
    files: list[AgentFile] = []
    for agent, root in _AGENT_ROOTS.items():
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in _EDITABLE_SUFFIXES:
                continue
            relative = path.relative_to(_FOLLOWUP_ROOT)
            if _is_hidden_or_cache(relative):
                continue
            files.append(
                AgentFile(
                    path=str(relative),
                    agent=agent,
                    folder=relative.parent.name,
                    title=_humanize(path.stem),
                    category=_category_for(relative),
                    preview=_preview_of(path),
                )
            )
    return files


@router.get("/content", response_model=AgentFileContent)
def read_agent_file(path: str) -> AgentFileContent:
    """Return the UTF-8 text content of one agent file."""
    candidate = _resolve_within_roots(path)
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content = candidate.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="File is not UTF-8 text")
    return AgentFileContent(path=path, content=content)


@router.put("/content", response_model=AgentFileContent)
def write_agent_file(request: SaveAgentFileRequest) -> AgentFileContent:
    """Overwrite one agent file with new content (must already exist)."""
    candidate = _resolve_within_roots(request.path)
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if len(request.content.encode("utf-8")) > _MAX_WRITE_BYTES:
        raise HTTPException(status_code=413, detail="File content is too large")

    candidate.write_text(request.content, encoding="utf-8")
    return AgentFileContent(path=request.path, content=request.content)


__all__ = ["router"]
