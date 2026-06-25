"""Composite and workflow tools for the CRM agent layer.

Two sub-modules:

- ``composite_reads`` — fan-out read tools that aggregate several bridge
  lookups into one structured response (``get_company_overview``,
  ``get_entity_timeline``, ``get_related_entities``, ``search_all_records``,
  ``get_pipeline_stages``).  These classify as READ and are wired into the
  ReaderWorker.

- ``workflows`` — high-level compound write workflows that collapse a full
  multi-step CRM operation into a single tool call (``onboard_new_client``,
  ``close_deal``, ``change_company_budget``, …).  These classify as WRITE and
  are wired into the WriterWorker.

Both modules expose ``build_*_tools(scope)`` factories that close over the
provided ``ToolScope`` for identity injection and scope enforcement, matching
the pattern established by ``agent.crm_tools.build_crm_tools``.
"""

from agent.tools.composite_reads import build_composite_read_tools
from agent.tools.workflows import build_workflow_tools

__all__ = [
    "build_composite_read_tools",
    "build_workflow_tools",
]
