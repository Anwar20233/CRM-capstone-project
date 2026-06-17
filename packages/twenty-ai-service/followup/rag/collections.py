"""Shared RAG collection identifiers (Person 1 infra).

NOTE: Minimal placeholder so Person 2/4 can reference collections ahead of
the full vector-store implementation. See
FOLLOWUP_INTELLIGENCE_LAYER_FINAL_SPEC.md §6.6.
"""

from __future__ import annotations

from enum import Enum


class CollectionName(str, Enum):
    """Logical document collections served by `RetrievalService`."""

    SALES_PLAYBOOKS = "sales_playbooks"
    BANT = "bant"
    EMAIL_TEMPLATES = "email_templates"
    PROPOSAL_TEMPLATES = "proposal_templates"
    PRODUCT_CATALOG = "product_catalog"
    SERVICE_CATALOG = "service_catalog"
    INDUSTRY_EXAMPLES = "industry_examples"
