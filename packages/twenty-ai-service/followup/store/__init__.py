"""Persistence layer for the Follow-Up Intelligence Agent.

Tables live in the `followup_agent` schema of Twenty's `default` database.
"""

from followup.store.repositories import (
    Database,
    ExtractionLogRepository,
    FollowupRun,
    PendingAction,
    PendingActionRepository,
    ProfileExtraction,
    ProfileFact,
    ProfileFactRepository,
    ProfileRelationship,
    ProfileRelationshipRepository,
    RunLogRepository,
    ShadowEntity,
    ShadowEntityRepository,
    apply_migrations,
)

__all__ = [
    "Database",
    "apply_migrations",
    "ProfileFact",
    "ProfileRelationship",
    "ShadowEntity",
    "ProfileExtraction",
    "PendingAction",
    "FollowupRun",
    "ProfileFactRepository",
    "ProfileRelationshipRepository",
    "ShadowEntityRepository",
    "ExtractionLogRepository",
    "PendingActionRepository",
    "RunLogRepository",
]
