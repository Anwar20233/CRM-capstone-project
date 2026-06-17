"""Profile read + write path for the Follow-Up Intelligence Agent.

Public surface:

* ``extract_from_source`` / ``ExtractionResult`` — the pipeline entry point.
* ``resolve_unknown_persons`` / ``ResolutionResult`` — entity resolution.
* ``create_shadow`` / ``merge_shadows`` / ``check_and_auto_promote`` — shadow
  entity lifecycle.
* ``ProfileService`` / ``ProfileNarrative`` / ``DealContext`` — the read path
  (profile synthesis).
* ``PipelineDeps`` and the service interfaces — for wiring and testing.
"""

from followup.profile.dependencies import (
    CRMOrchestrator,
    CRMReader,
    LoggingNotificationService,
    NotificationService,
    PipelineDeps,
)
from followup.profile.extraction import (
    ExtractionResult,
    FollowupExtractionOutcome,
    extract_from_email,
    extract_from_source,
)
from followup.profile.resolution import ResolutionResult, resolve_unknown_persons
from followup.profile.schemas import (
    ContactSummary,
    DealContext,
    ProfileNarrative,
)
from followup.profile.service import ProfileNotFound, ProfileService
from followup.profile.shadow import (
    check_and_auto_promote,
    create_shadow,
    merge_shadows,
)

__all__ = [
    "extract_from_email",
    "extract_from_source",
    "ExtractionResult",
    "FollowupExtractionOutcome",
    "resolve_unknown_persons",
    "ResolutionResult",
    "create_shadow",
    "merge_shadows",
    "check_and_auto_promote",
    "PipelineDeps",
    "CRMReader",
    "CRMOrchestrator",
    "NotificationService",
    "LoggingNotificationService",
    "ProfileService",
    "ProfileNotFound",
    "ProfileNarrative",
    "DealContext",
    "ContactSummary",
]
