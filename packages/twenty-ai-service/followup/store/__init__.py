"""Persistence layer for the Follow-Up Intelligence Agent.

Tables live in the `followup_agent` schema of Twenty's `default` database.
"""

from followup.store.repositories import (
    Database,
    ExtractionLogRepository,
    FollowupRun,
    InboundEmail,
    InboundEmailRepository,
    PendingAction,
    PendingActionRepository,
    ProfileExtraction,
    ProfileFact,
    ProfileFactRepository,
    ProfileRelationship,
    ProfileRelationshipRepository,
    RiskDailyScore,
    RiskDailyScoreRepository,
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
    "RiskDailyScore",
    "InboundEmail",
    "ProfileFactRepository",
    "ProfileRelationshipRepository",
    "ShadowEntityRepository",
    "ExtractionLogRepository",
    "PendingActionRepository",
    "RiskDailyScoreRepository",
    "InboundEmailRepository",
    "RunLogRepository",
]
