"""External-service interfaces and the dependency bundle for the pipeline.

The extraction pipeline depends on three things it does not own:

* **CRM Reader** — reads the contacts, company, and opportunity already in
  Twenty (so the LLM can attribute facts to real records).
* **CRM Orchestrator** — the write path, used only to promote a shadow entity
  into a real CRM contact.
* **Notification service** — tells the rep when the agent auto-added a contact.

Each is declared as a ``Protocol`` so the pipeline is wired by injection: tests
pass in-memory fakes, production passes the bridge-backed adapters below. The
``PipelineDeps`` dataclass bundles those services together with the persistence
repositories (built from one database executor) and the chat model, so every
node and helper receives a single ``deps`` object.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

from langchain_core.language_models.chat_models import BaseChatModel

if TYPE_CHECKING:
    from followup.calendar.reader import CalendarReader

from followup.store.repositories import (
    ExtractionLogRepository,
    PendingActionRepository,
    ProfileFactRepository,
    ProfileRelationshipRepository,
    RiskSnapshotRepository,
    RunLogRepository,
    ShadowEntityRepository,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# Service interfaces
# ===========================================================================


@runtime_checkable
class CRMReader(Protocol):
    """Reads existing CRM records, anchored on the email sender.

    The follow-up agent rarely knows the opportunity up front — an email just
    arrives. So the primary entry is ``get_person_by_email`` (sender → person →
    company), from which the deal candidates and contacts follow. Only the
    ``load_context`` graph node calls these methods.
    """

    async def get_person_by_email(self, email: str) -> Optional[dict[str, Any]]:
        """Resolve a sender email to a person.

        Returns ``{id, name, role, company_id, company_name}`` — the strongest
        signal for "who does the agent work with" — or ``None`` if no CRM person
        owns that email.
        """
        ...

    async def get_company(self, company_id: str) -> Optional[dict[str, Any]]:
        """Return the company as ``{id, name}`` (or ``None`` if not found)."""
        ...

    async def get_opportunity(
        self, opportunity_id: str
    ) -> Optional[dict[str, Any]]:
        """Return one opportunity as ``{id, name, stage, value, company_id}``.

        ``value`` is the deal amount in dollars (the adapter converts Twenty's
        ``amount.amountMicros`` / 1_000_000). The read path (Step 3) needs it for
        ``DealContext``; the extraction path ignores it.
        """
        ...

    async def get_open_opportunities_for_company(
        self, company_id: str
    ) -> list[dict[str, Any]]:
        """Return the company's open deals as ``{id, name, stage, point_of_contact_id}``.

        These are the candidate opportunities the extractor chooses between.
        """
        ...

    async def get_contacts_for_company(
        self, company_id: str
    ) -> list[dict[str, Any]]:
        """Return the company's people as ``{id, name, role, company, email}``."""
        ...

    async def get_activities_for_opportunity(
        self, opportunity_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return the deal's recent timeline as ``{type, date, summary}`` items.

        Backed by the reader's unified notes+tasks timeline, newest-first. Used
        by the read path (Step 3) to surface the last activity in the briefing.
        """
        ...


@runtime_checkable
class CRMOrchestrator(Protocol):
    """The CRM write path — used here only to promote a shadow to a contact."""

    async def create_contact(
        self,
        name: str,
        email: Optional[str],
        role: Optional[str],
        company_id: Optional[str],
        workspace_id: str,
        initiated_by: str = "followup_agent",
    ) -> dict[str, Any]:
        """Create a person in Twenty CRM; return the created record (with ``id``)."""
        ...


@runtime_checkable
class NotificationService(Protocol):
    """Notifies the sales rep about agent-initiated changes."""

    async def notify_rep(
        self,
        workspace_id: str,
        opportunity_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        ...


# ===========================================================================
# Default adapters
# ===========================================================================


class LoggingNotificationService:
    """Default notifier: logs the event and keeps it in memory.

    TODO: replace with the real rep-notification channel once it exists (a
    notification record, websocket push, or email). The interface above is the
    contract that real service must satisfy. ``events`` is exposed so tests and
    early integrations can assert what would have been sent.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def notify_rep(
        self,
        workspace_id: str,
        opportunity_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        event = {
            "workspace_id": workspace_id,
            "opportunity_id": opportunity_id,
            "event_type": event_type,
            "payload": payload,
        }
        self.events.append(event)
        logger.info("follow-up notification: %s", event)


class ReaderAgentCRMReader:
    """CRM Reader that delegates to the existing CRM Read agent (``ReaderWorker``).

    This is the follow-up agent's single point of contact with the reader. Only
    the ``load_context`` graph node calls these methods; nothing else in the
    pipeline talks to the reader. Each method sends ONE natural-language
    instruction to the reader agent and parses the structured JSON resolution
    response it returns (the reader resolves names to real ids/records and
    unmasks them, which is exactly the "get the actual ids and real names" step).

    TODO: the person/company field paths read below (``name.firstName``,
    ``emails.primaryEmail``, ``jobTitle``) follow Twenty's standard schema;
    confirm them against the reader's live output before production. Unit tests
    inject a fake reader instead.
    """

    def __init__(
        self, session_id: str = "followup-extraction", model: Optional[str] = None
    ) -> None:
        self._session_id = session_id
        self._model = model

    async def _run_reader(self, instruction: str) -> dict[str, Any]:
        # A fresh reader per call keeps this stateless; the reader owns its own
        # masking/unmasking, so the JSON we get back already carries real values.
        from agent.workers.reader_worker import ReaderWorker

        reader = ReaderWorker(session_id=self._session_id, model=self._model)
        return await reader.run(instruction)

    def _records_from(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        """Records from a reader run, preferring its structured resolution.

        This model frequently emits a malformed final ``response`` (a truncated
        tool-call fragment) even though the underlying ``find_*`` executed fine.
        So when the resolution JSON yields nothing, fall back to mining the real
        records out of the executed tool calls — deterministic and reliable, no
        LLM re-roll needed.
        """
        from followup.profile.llm import parse_json_response

        records = _reader_records(parse_json_response(result.get("response", "")))
        if records:
            return records
        return _records_from_tool_calls(result.get("tool_calls"))

    async def _ask(self, instruction: str) -> Any:
        # Structured-resolution view only (kept for callers that read the
        # reader's own ``resolution`` envelope, e.g. nested company on a person).
        from followup.profile.llm import parse_json_response

        return parse_json_response(
            (await self._run_reader(instruction)).get("response", "")
        )

    async def _ask_for_records(
        self, instruction: str, attempts: int = 2
    ) -> list[dict[str, Any]]:
        # Resolution-first, tool-call-fallback. One retry covers the rare case
        # where even the find itself didn't run.
        records: list[dict[str, Any]] = []
        for _ in range(max(1, attempts)):
            records = self._records_from(await self._run_reader(instruction))
            if records:
                break
        return records

    async def _ask_for_record(
        self, instruction: str, attempts: int = 2
    ) -> Optional[dict[str, Any]]:
        records = await self._ask_for_records(instruction, attempts)
        return records[0] if records else None

    async def _bridge_find(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        # Direct read for lookups keyed on an id we already hold. The reader
        # agent exists to resolve names → real ids and unmask them; when we
        # already have the real id (the read path always does), routing through
        # the LLM only adds latency and this model's find_one flakiness. A
        # straight bridge find is deterministic. Read-only scope is enforced.
        from agent.tool_scope import READER_SCOPE
        from agent.tools.composite_reads import _exec, _identity
        from bridge_client import forward

        result = await forward("execute", _exec(tool, args, _identity(READER_SCOPE)))
        if not result.get("ok"):
            logger.warning("bridge %s failed: %s", tool, result.get("error"))
            return []
        return _records_from_bridge_data(result.get("data"))

    async def get_person_by_email(self, email: str) -> Optional[dict[str, Any]]:
        data = await self._ask(
            f"Find the person whose email address is {email}. "
            f"Return their id, name, job title, and company (id and name)."
        )
        record = _reader_single_record(data)
        if record is None:
            return None
        contact = _reader_person_to_contact(record)
        company = record.get("company") if isinstance(record.get("company"), dict) else {}
        return {
            "id": contact["id"],
            "name": contact["name"],
            "role": contact["role"],
            "company_id": company.get("id") or record.get("companyId"),
            "company_name": company.get("name"),
        }

    async def get_opportunity(
        self, opportunity_id: str
    ) -> Optional[dict[str, Any]]:
        records = await self._bridge_find("find_one_opportunity", {"id": opportunity_id})
        if not records:
            return None
        record = records[0]
        company = record.get("company") or {}
        return {
            "id": record.get("id", opportunity_id),
            "name": record.get("name"),
            "stage": record.get("stage"),
            "value": _reader_amount_to_dollars(record.get("amount")),
            "company_id": company.get("id") or record.get("companyId"),
        }

    async def get_company(self, company_id: str) -> Optional[dict[str, Any]]:
        records = await self._bridge_find("find_one_company", {"id": company_id})
        if not records:
            return None
        record = records[0]
        return {"id": record.get("id", company_id), "name": record.get("name")}

    async def get_open_opportunities_for_company(
        self, company_id: str
    ) -> list[dict[str, Any]]:
        # Direct read by company id (we hold it). Twenty's opportunity_stage
        # enum has no closed/won/lost value, so there is nothing to exclude —
        # every deal on the company is "open"; the extractor picks which one.
        records = await self._bridge_find(
            "find_opportunities", {"limit": 50, "companyId": {"eq": company_id}}
        )
        return [
            {
                "id": record.get("id"),
                "name": record.get("name"),
                "stage": record.get("stage"),
                "point_of_contact_id": (record.get("pointOfContact") or {}).get("id")
                if isinstance(record.get("pointOfContact"), dict)
                else record.get("pointOfContactId"),
            }
            for record in records
        ]

    async def get_contacts_for_company(
        self, company_id: str
    ) -> list[dict[str, Any]]:
        records = await self._bridge_find(
            "find_people", {"limit": 50, "companyId": {"eq": company_id}}
        )
        return [_reader_person_to_contact(record) for record in records]

    async def get_activities_for_opportunity(
        self, opportunity_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        # Notes + tasks targeting this opportunity, merged newest-first — the
        # same shape as the reader's timeline composite, fetched directly. An
        # empty timeline is a legitimate answer (a quiet deal).
        # orderBy is an array of single-key objects (the backend does
        # `(orderBy ?? []).filter(...)`, so an object value throws).
        notes = await self._bridge_find(
            "find_notes",
            {
                "limit": limit,
                "noteTargets": {"some": {"opportunityId": {"eq": opportunity_id}}},
                "orderBy": [{"updatedAt": "DescNullsLast"}],
            },
        )
        tasks = await self._bridge_find(
            "find_tasks",
            {
                "limit": limit,
                "taskTargets": {"some": {"opportunityId": {"eq": opportunity_id}}},
                "orderBy": [{"updatedAt": "DescNullsLast"}],
            },
        )
        activities = [_activity_from_record("note", r) for r in notes]
        activities += [_activity_from_record("task", r) for r in tasks]
        activities.sort(key=lambda a: a.get("date") or "", reverse=True)
        return activities[:limit]


def _reader_amount_to_dollars(amount: Any) -> float:
    """Twenty's ``amount.amountMicros`` (or a bare number) → float dollars."""
    if isinstance(amount, dict):
        micros = amount.get("amountMicros")
        if micros is not None:
            try:
                return float(micros) / 1_000_000
            except (TypeError, ValueError):
                return 0.0
        amount = amount.get("value")
    try:
        return float(amount)
    except (TypeError, ValueError):
        return 0.0


def _activity_from_record(kind: str, record: dict[str, Any]) -> dict[str, Any]:
    """A note/task bridge record → ``{type, date, summary}`` timeline item."""
    body = record.get("bodyV2") or record.get("body")
    if isinstance(body, dict):
        body = body.get("markdown") or body.get("blocknote")
    return {
        "type": kind,
        "date": record.get("createdAt")
        or record.get("updatedAt")
        or record.get("dueAt"),
        "summary": record.get("title") or body,
    }


def _records_from_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    """Mine real records out of the reader's executed tool-call log.

    Fallback for when the reader's final ``response`` text is malformed but a
    ``find_*`` actually ran. Walks the calls newest-first and returns the first
    ``find`` result that carries records, so the answer reflects the final
    lookup rather than an intermediate catalog/schema probe.
    """
    if not isinstance(tool_calls, list):
        return []
    for call in reversed(tool_calls):
        if not isinstance(call, dict):
            continue
        tool_name = _tool_name_of(call)
        if not tool_name or not tool_name.startswith("find"):
            continue
        result = call.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        records = _records_from_bridge_data(data)
        if records:
            return records
    return []


def _tool_name_of(call: dict[str, Any]) -> Optional[str]:
    """The CRM tool a tool-call entry ran (unwrapping the execute_tool wrapper)."""
    name = call.get("name")
    if name in ("execute_tool", "execute"):
        args = call.get("args")
        return args.get("tool") if isinstance(args, dict) else None
    return name


def _records_from_bridge_data(data: Any) -> list[dict[str, Any]]:
    """Records from a bridge find/find_one envelope (``data.result.records``)."""
    if not isinstance(data, dict):
        return []
    result = data.get("result")
    if isinstance(result, dict):
        records = result.get("records")
        if isinstance(records, list):
            return [r for r in records if isinstance(r, dict)]
        if result.get("id"):
            return [result]
    return []


def _reader_records(data: Any) -> list[dict[str, Any]]:
    """Pull records out of the reader's resolution JSON (single/multiple/none)."""
    if not isinstance(data, dict):
        return []
    resolution = data.get("resolution")
    if resolution == "single" and isinstance(data.get("record"), dict):
        return [data["record"]]
    if resolution == "multiple" and isinstance(data.get("candidates"), list):
        return [c for c in data["candidates"] if isinstance(c, dict)]
    return []


def _reader_single_record(data: Any) -> Optional[dict[str, Any]]:
    """The first record from a reader response, or ``None`` for no match."""
    records = _reader_records(data)
    return records[0] if records else None


def _reader_person_to_contact(person: dict[str, Any]) -> dict[str, Any]:
    """Flatten a reader person record into the pipeline's contact shape."""
    name = person.get("name")
    if isinstance(name, dict):
        full_name = " ".join(
            part for part in (name.get("firstName"), name.get("lastName")) if part
        )
    else:
        full_name = str(name) if name else None
    emails = person.get("emails")
    email = emails.get("primaryEmail") if isinstance(emails, dict) else person.get("email")
    company = person.get("company")
    company_name = company.get("name") if isinstance(company, dict) else None
    return {
        "id": person.get("id"),
        "name": full_name or None,
        "role": person.get("jobTitle") or person.get("role"),
        "company": company_name,
        "email": email,
    }


def _bridge_created_id(data: Any) -> Optional[str]:
    """Pull the new record id out of the bridge's create envelope.

    Seen shapes: ``{result: {id}}``, ``{recordReferences: [{recordId}]}``, and
    a bare ``{id}``. Returns ``None`` if none are present.
    """
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    if isinstance(result, dict) and result.get("id"):
        return result["id"]
    refs = data.get("recordReferences")
    if isinstance(refs, list) and refs and isinstance(refs[0], dict):
        record_id = refs[0].get("recordId")
        if record_id:
            return record_id
    return data.get("id")


class BridgeCRMOrchestrator:
    """CRM Orchestrator adapter that creates a person through the bridge.

    TODO: route through the real writer/orchestrator (with its write-policy
    gating) rather than calling ``create_person`` directly once that programmatic
    entry point is available. The ``initiated_by`` tag is recorded so promoted
    contacts are auditable as agent-created.
    """

    def __init__(self, scope: Any = None) -> None:
        from agent.tool_scope import WRITER_SCOPE

        self._scope = scope or WRITER_SCOPE

    async def create_contact(
        self,
        name: str,
        email: Optional[str],
        role: Optional[str],
        company_id: Optional[str],
        workspace_id: str,
        initiated_by: str = "followup_agent",
    ) -> dict[str, Any]:
        from agent.tools.composite_reads import _exec, _identity
        from bridge_client import forward

        first, _, last = (name or "").partition(" ")
        args: dict[str, Any] = {"name": {"firstName": first, "lastName": last}}
        if role:
            args["jobTitle"] = role
        if email:
            args["emails"] = {"primaryEmail": email}
        if company_id:
            args["companyId"] = company_id

        ident = _identity(self._scope)
        result = await forward("execute", _exec("create_person", args, ident))
        if not result.get("ok"):
            error = result.get("error", {})
            raise RuntimeError(
                f"create_contact failed: {error.get('message', 'unknown error')}"
            )
        # The bridge's create envelope nests the new id at data.result.id (with a
        # mirror in recordReferences) — it is NOT top-level like the find shape.
        # Normalize to the {id, name, role, email} record the contract promises.
        data = result.get("data") or {}
        created_id = _bridge_created_id(data)
        if created_id is None:
            raise RuntimeError(f"create_contact: no id in bridge response: {data}")
        return {"id": created_id, "name": name, "role": role, "email": email}


# ===========================================================================
# Dependency bundle
# ===========================================================================


@dataclass
class PipelineDeps:
    """Everything the pipeline needs, assembled once per run.

    ``executor`` is any asyncpg pool/connection/``Database`` the repositories
    accept. The repositories are built from it in ``__post_init__`` so callers
    pass a single executor, not five repositories. ``chat_llm`` is built lazily
    from env on first use unless one is injected (tests inject a fake).
    """

    executor: Any
    crm_reader: CRMReader
    crm_orchestrator: CRMOrchestrator
    notifier: NotificationService
    model: Optional[str] = None
    # The orchestrator's OWN reasoning (classify, content authoring) can run on a
    # different, smarter model than the worker model (``model``, used by the
    # reader). Falls back to ``model`` when unset.
    chat_model: Optional[str] = None
    chat_llm: Optional[BaseChatModel] = None

    # Calendar is a distinct read subdomain (Step 5). Defaults to the bridge
    # adapter, built lazily in create(); tests inject a FakeCalendarReader.
    calendar_reader: "CalendarReader" = None  # type: ignore[assignment]

    # Repositories default to ones built from ``executor``; tests inject fakes.
    facts: ProfileFactRepository = None  # type: ignore[assignment]
    relationships: ProfileRelationshipRepository = None  # type: ignore[assignment]
    shadows: ShadowEntityRepository = None  # type: ignore[assignment]
    extractions: ExtractionLogRepository = None  # type: ignore[assignment]
    risk_snapshots: RiskSnapshotRepository = None  # type: ignore[assignment]
    # Step 6 (orchestrator) write targets: the pending action it persists for
    # rep review and the run log it records.
    pending_actions: PendingActionRepository = None  # type: ignore[assignment]
    runs: RunLogRepository = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.facts is None:
            self.facts = ProfileFactRepository(self.executor)
        if self.relationships is None:
            self.relationships = ProfileRelationshipRepository(self.executor)
        if self.shadows is None:
            self.shadows = ShadowEntityRepository(self.executor)
        if self.extractions is None:
            self.extractions = ExtractionLogRepository(self.executor)
        if self.risk_snapshots is None:
            self.risk_snapshots = RiskSnapshotRepository(self.executor)
        if self.pending_actions is None:
            self.pending_actions = PendingActionRepository(self.executor)
        if self.runs is None:
            self.runs = RunLogRepository(self.executor)
        if self.calendar_reader is None:
            from followup.calendar.reader import BridgeCalendarReader

            self.calendar_reader = BridgeCalendarReader()

    def get_chat_llm(self) -> BaseChatModel:
        """Return the orchestrator's chat model, building it from env on first use.

        Uses ``chat_model`` (the orchestrator's own model) when set, else the
        worker ``model``.
        """
        if self.chat_llm is None:
            from followup.profile.llm import build_chat_llm

            self.chat_llm = build_chat_llm(self.chat_model or self.model)
        return self.chat_llm

    @classmethod
    def create(
        cls,
        executor: Any,
        model: Optional[str] = None,
        chat_model: Optional[str] = None,
    ) -> "PipelineDeps":
        """Build deps with the default bridge-backed adapters.

        ``model`` is the worker model (used by the reader); ``chat_model`` is the
        orchestrator's own reasoning model (classify, content authoring) when it
        should differ from the worker model.
        """
        # Real runs load the PII NER models once here so the pipeline can mask
        # not-yet-known names; unit tests build PipelineDeps directly and skip it.
        from followup.profile.masking import ensure_models_loaded

        ensure_models_loaded()
        return cls(
            executor=executor,
            crm_reader=ReaderAgentCRMReader(model=model),
            crm_orchestrator=BridgeCRMOrchestrator(),
            notifier=LoggingNotificationService(),
            model=model,
            chat_model=chat_model,
        )
