from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

SectionStatus = Literal["loaded", "partial", "unavailable", "not_requested"]


class ContextSectionStatus(BaseModel):
    status: SectionStatus = "loaded"
    reason: str | None = None
    source: str | None = None


class ContextCompleteness(BaseModel):
    opportunity: ContextSectionStatus = ContextSectionStatus(status="loaded")
    company: ContextSectionStatus = ContextSectionStatus(status="loaded")
    contacts: ContextSectionStatus = ContextSectionStatus(status="loaded")
    timeline: ContextSectionStatus = ContextSectionStatus(status="loaded")
    tasks: ContextSectionStatus = ContextSectionStatus(status="loaded")
    meetings: ContextSectionStatus = ContextSectionStatus(status="loaded")
    pipeline_metadata: ContextSectionStatus = ContextSectionStatus(status="loaded")


def section_loaded(*, source: str | None = None) -> ContextSectionStatus:
    return ContextSectionStatus(status="loaded", source=source)


def section_partial(
    reason: str,
    *,
    source: str | None = None,
) -> ContextSectionStatus:
    return ContextSectionStatus(status="partial", reason=reason, source=source)


def section_unavailable(
    reason: str,
    *,
    source: str = "agent_bridge",
) -> ContextSectionStatus:
    return ContextSectionStatus(status="unavailable", reason=reason, source=source)


def section_not_requested() -> ContextSectionStatus:
    return ContextSectionStatus(status="not_requested")


def build_bridge_fetch_completeness(
    *,
    has_company: bool,
    has_contact: bool,
    pipeline_source: str | None,
    has_scalar_timeline: bool,
) -> ContextCompleteness:
    timeline_status = section_partial(
        (
            "Opportunity scalar emailText/notes loaded, but linked CRM "
            "activity records were unavailable."
        ),
        source="opportunity_scalar_fields" if has_scalar_timeline else "agent_bridge",
    )
    if not has_scalar_timeline:
        timeline_status = section_unavailable(
            "No opportunity timeline content was available.",
        )

    pipeline_status = (
        section_loaded(source=pipeline_source)
        if pipeline_source
        else section_unavailable(
            "Pipeline stage metadata could not be loaded.",
            source="agent_bridge",
        )
    )

    return ContextCompleteness(
        opportunity=section_loaded(source="agent_bridge"),
        company=(
            section_loaded(source="agent_bridge")
            if has_company
            else section_unavailable(
                "No company is linked to this opportunity.",
                source="agent_bridge",
            )
        ),
        contacts=(
            section_loaded(source="agent_bridge")
            if has_contact
            else section_unavailable(
                "No point of contact is linked to this opportunity.",
                source="agent_bridge",
            )
        ),
        timeline=timeline_status,
        tasks=section_unavailable(
            "Reader tool does not expose the required opportunity relationship filter.",
            source="agent_bridge",
        ),
        meetings=section_unavailable(
            "No supported meeting query is currently configured.",
            source="agent_bridge",
        ),
        pipeline_metadata=pipeline_status,
    )


def section_status(
    completeness: ContextCompleteness | None,
    section: Literal[
        "opportunity",
        "company",
        "contacts",
        "timeline",
        "tasks",
        "meetings",
        "pipeline_metadata",
    ],
) -> SectionStatus:
    if completeness is None:
        return "loaded"
    return getattr(completeness, section).status
