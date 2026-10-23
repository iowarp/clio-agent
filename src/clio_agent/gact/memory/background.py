"""The background pass: after a conversation goes idle, propose what to remember.

The scheduler's minute tick calls :func:`memory_tick`, which does two things per root
conversation (or fork) and never runs its own loop:

1. **Start.** A session idle for ``memory.idle_minutes`` with at least
   :data:`MIN_USER_MESSAGES` user messages past its watermark is read by the extract
   stage (:mod:`.extract`, fleet role ``memory_extract``). When it yields candidates,
   the consolidate stage starts: a ``curator`` session (the ``memory`` plugin's
   defined subagent, fleet role ``memory_consolidate``) with a root assignment whose
   handle is derived from the source session and watermark, and its input as
   delegation mail -- an ordinary task, so it shows in the task list, can be
   cancelled, and resumes on the same handle after a restart. A root assignment
   wakes nobody when it completes, so the user's conversation is never touched.
2. **Collect.** When the curator's assignment is terminal, the source session's
   watermark advances to the last message the pass read.

Skipped: background memory off, memory off, a session started with **Memory off**, a
workspace that opted out of account memory reading user scope only affects scopes,
and sessions that are not root conversations or forks. Expiry proposals ride on a
pass that touches a scope.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from clio_agent.errors import ClioError
from clio_agent.gact.memory.curator import CANDIDATES, ORIGIN, SCOPES
from clio_agent.gact.memory.policy import (
    SESSION_CURATOR,
    session_policy,
    setting,
)
from clio_agent.gact.session_kind import session_kind

logger = logging.getLogger(__name__)

#: Session metadata on the source conversation.
WATERMARK = "memory_watermark"  # id of the last message a finished pass read
IN_FLIGHT = "memory_pass"  # the running pass: curator session, handle, read-up-to id
LAST_PASS = "memory_last_pass"  # how the last pass ended (a failed one is not retried as is)
MIN_USER_MESSAGES = 3
CURATOR_TARGET = "curator"


class BackgroundPassError(ClioError):
    """The background pass could not start (typed reason)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message, error_type=reason, details={"reason": reason})


def _parse(stamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _messages_after(messages: list[Any], watermark: str) -> list[Any]:
    if not watermark:
        return list(messages)
    ids = [message.id for message in messages]
    return list(messages[ids.index(watermark) + 1 :]) if watermark in ids else list(messages)


def skip_reason(app: Any, session: Any, now: datetime) -> str:
    """Why ``session`` is not due for a background pass now (``""`` when it is)."""
    if session_kind(session) not in ("conversation", "branch"):
        return "not_a_conversation"
    metadata = session.metadata or {}
    if metadata.get(IN_FLIGHT):
        return "pass_in_flight"
    if metadata.get(SESSION_CURATOR):
        return "curator_session"
    failed = metadata.get(LAST_PASS) or {}
    messages = app.state.messages.get(session.id, [])
    if (
        failed.get("status", "completed") != "completed"
        and messages
        and failed.get("read_to") == messages[-1].id
    ):
        return "last_pass_failed"  # retried once the conversation has new messages
    policy = session_policy(app, session)
    if not (policy.enabled and policy.may_write):
        return policy.reason or "memory_not_writable"
    if session.status not in ("idle", "error", "cancelled"):
        return "session_busy"
    last = _parse(str(session.last_interaction_at or session.updated_at or ""))
    idle = timedelta(minutes=int(setting("memory.idle_minutes", 30)))
    if last is None or now - last < idle:
        return "not_idle_yet"
    fresh = _messages_after(app.state.messages.get(session.id, []), metadata.get(WATERMARK, ""))
    if sum(1 for message in fresh if message.role == "user") < MIN_USER_MESSAGES:
        return "too_few_new_messages"
    return ""


def eligible(app: Any, session: Any, now: datetime) -> bool:
    """Whether ``session`` is due for a background pass now."""
    return not skip_reason(app, session, now)


def _curator_resolution(app: Any) -> tuple[dict[str, Any], str]:
    """``(pin metadata, agent id)`` of the ``memory`` plugin's ``curator`` subagent."""
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        AGENT_DELTA_KEY,
        AGENT_REF_KEY,
        AGENT_RESOLUTION_KEY,
        SUBAGENT_KEY,
        default_agent_ref,
        plugin_service,
        resolve_for_session,
    )

    parent = resolve_for_session(app, default_agent_ref(app))
    child = plugin_service(app).resolve_subagent(parent, CURATOR_TARGET)
    pin = {
        AGENT_REF_KEY: child.agent["ref"],
        AGENT_DELTA_KEY: {"add": [], "remove": []},
        AGENT_RESOLUTION_KEY: child.digest,
        SUBAGENT_KEY: CURATOR_TARGET,
    }
    return pin, str(child.agent["id"])


def _forgotten_facts(service: Any, scopes: tuple[str, ...]) -> list[str]:
    """Texts of facts the user asked to forget, from history (for the models only)."""
    wanted = {fid for scope in scopes for fid in service.forgotten(scope)}
    if not wanted:
        return []
    from clio_agent.arc.memory_document import MemoryDocumentError, parse  # noqa: PLC0415
    from clio_agent.gact.memory.store import VERSIONS  # noqa: PLC0415

    found: dict[str, str] = {}
    for scope in scopes:
        for name in service.store.scan_names(VERSIONS, scope + "/"):
            raw = service.store.get(VERSIONS, name)
            text = json.loads(raw).get("text", "") if raw else ""
            try:
                document = parse(text) if text else None
            except MemoryDocumentError:
                continue
            for fact in document.facts if document else ():
                if fact.id in wanted:
                    found[fact.id] = fact.text
    return sorted(found.values())


def _curator_brief(
    candidates: list[dict[str, Any]],
    *,
    forgotten: list[str],
    rejected: list[str],
    documents: list[dict[str, Any]],
) -> str:
    return "Curate long-term memory from these candidates.\n\n" + json.dumps(
        {
            "candidates": candidates,
            "forgotten": forgotten,
            "rejected": rejected,
            "documents": documents,
        },
        indent=1,
    )


def start_curator(
    app: Any,
    *,
    origin: str,
    source_session: Any | None,
    workspace_id: str,
    scopes: tuple[str, ...],
    candidates: list[dict[str, Any]],
    run_key: str,
) -> dict[str, str]:
    """Create (or find) the curator session and its root assignment; return its address.

    Idempotent per ``run_key``: the session id, assignment handle and input mail id are
    all derived from it, so a repeated call (a restart between ticks) re-attaches.
    """
    from clio_agent.arc.task_types import MailItem  # noqa: PLC0415
    from clio_agent.gact.memory.proposals import ProposalStore  # noqa: PLC0415
    from clio_agent.gact.memory.runtime import memory_service, memory_usage  # noqa: PLC0415
    from clio_agent.gact.memory.usage import document_subject  # noqa: PLC0415
    from clio_agent.gact.native_work import now  # noqa: PLC0415

    service = memory_service(app)
    usage = memory_usage(app)
    pin, agent_id = _curator_resolution(app)
    rejected = [
        f"{p.target}: {'; '.join(p.facts)}"
        for p in ProposalStore(service).all()
        if p.status == "rejected"
    ]
    expiry = timedelta(days=int(setting("memory.expiry_days", 60)))
    cutoff = (datetime.now(UTC) - expiry).strftime("%Y-%m-%dT%H:%M:%SZ")
    documents, retire = [], []
    for stored in service.documents(scopes):
        counted = usage.get(document_subject(str(stored.name)))
        documents.append(
            {
                "name": str(stored.name),
                "description": stored.document.description,
                "read": counted.read,
                "last_read": counted.last_read,
            }
        )
        last = counted.last_read or stored.document.updated
        if stored.name.category not in ("profile", "preferences") and last < cutoff:
            retire.append(str(stored.name))
    proposals = ProposalStore(service)
    for name in retire:  # the expiry rule: a document nobody read for expiry_days
        last = usage.get(document_subject(name)).last_read or "never"
        proposals.propose(
            target=name,
            change="retire",
            facts=[],
            replaces=[],
            merge_from=[],
            description="",
            evidence=[],
            rationale=f"Not read since {last}.",
            origin=f"expiry:{name}:{last}",
            handle="",
            auto_accept="none",
        )
    brief = _curator_brief(
        candidates,
        forgotten=_forgotten_facts(service, scopes),
        rejected=rejected,
        documents=documents,
    )
    human = str(app.state.human_principal)
    metadata = {
        **pin,
        "session_type": "agent_task",
        SESSION_CURATOR: True,
        CANDIDATES: candidates,
        SCOPES: list(scopes),
        ORIGIN: origin,
        "model_role": "memory_consolidate",
        "owner_human_id": human,
    }
    creation = f"memory-curate:{run_key}"
    digest = hashlib.sha256(json.dumps([creation, brief], sort_keys=True).encode()).hexdigest()
    session = app.state.sessions.create(
        workspace_id=workspace_id,
        title="Memory curation",
        metadata=metadata,
        parent_session_id=source_session.id if source_session is not None else "",
        agent={"id": agent_id},
        creation_id=creation,
        creation_digest=digest,
        defer_initialization=True,
    )
    handle = "task_" + hashlib.sha256(f"{session.id}\0memory-curate".encode()).hexdigest()[:32]
    history, owner = app.state.task_history, app.state.conversations
    with history.serialized(writing=True):
        if history.task_snapshot(handle) is None:
            owner._new_assignment(  # noqa: SLF001 - the sanctioned root-assignment path
                session.id,
                "Curate long-term memory from a conversation.",
                human,
                invocation_id=creation,
                handle=handle,
            )
        mail_id = "assignment:" + handle
        if history.mail_item(session.id, mail_id) is None:
            # Its own kind, not "delegation": a delegation renders from its parent's
            # assignment, and the curator's is a root assignment with no parent.
            content = history.write_content(session.id, brief)
            history.record_mail(
                MailItem(
                    id=mail_id,
                    recipient_session=session.id,
                    sender_session=session.id,
                    kind="memory",
                    content=content,
                    created_at=now(),
                    handle=handle,
                )
            )
    return {"session": session.id, "handle": handle}  # recording the mail signals its runner


def _start_pass(app: Any, session: Any) -> None:
    from clio_agent.gact.memory.extract import extract_candidates, transcript_items  # noqa: PLC0415
    from clio_agent.gact.memory.runtime import memory_service  # noqa: PLC0415

    metadata = session.metadata or {}
    messages = app.state.messages.get(session.id, [])
    items = transcript_items(messages, after=str(metadata.get(WATERMARK, "")))
    last = messages[-1].id if messages else ""
    policy = session_policy(app, session)
    service = memory_service(app)
    candidates, report = extract_candidates(
        items,
        session_id=session.id,
        forgotten=_forgotten_facts(service, policy.scopes),
        include_sensitive=bool(setting("memory.sensitive", False)),
    )
    logger.info("memory pass session=%s extract=%s", session.id, report)
    if not candidates:  # nothing worth keeping: the pass is done, and says so
        empty = {"read_to": last, "extract": report}
        patch = collect_patch(empty, "no_candidates", datetime.now(UTC))
        app.state.sessions.update(session.id, metadata_patch={**patch, WATERMARK: last})
        return
    run = start_curator(
        app,
        origin=f"session:{session.id}",
        source_session=session,
        workspace_id=session.workspace_id,
        scopes=policy.scopes,
        candidates=[candidate.to_record() for candidate in candidates],
        run_key=f"{session.id}:{last}",
    )
    app.state.sessions.update(
        session.id,
        metadata_patch={IN_FLIGHT: {**run, "read_to": last, "extract": report}},
    )


def collect_patch(run: dict[str, Any], status: str, now: datetime) -> dict[str, Any]:
    """The source session's metadata once its pass ended with ``status``.

    Only a completed pass moves the watermark; a failed one keeps its messages.
    """
    patch: dict[str, Any] = {
        IN_FLIGHT: None,
        LAST_PASS: {
            "handle": run.get("handle", ""),
            "session": run.get("session", ""),
            "status": status,
            "read_to": run.get("read_to", ""),
            "extract": run.get("extract", {}),  # model and counts, kept for the audit
            "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }
    if status == "completed":
        patch[WATERMARK] = run.get("read_to", "")
    return patch


def _collect(app: Any, session: Any) -> None:
    run = (session.metadata or {}).get(IN_FLIGHT) or {}
    task = app.state.task_history.task_snapshot(str(run.get("handle", "")))
    if task is not None and task.terminal_at is None:
        return
    status = task.status if task is not None else "missing"
    patch = collect_patch(run, status, datetime.now(UTC))
    if WATERMARK not in patch:
        logger.warning(
            "memory pass session=%s curator=%s ended status=%s; its messages stay unread",
            session.id,
            run.get("session", ""),
            status,
        )
    app.state.sessions.update(session.id, metadata_patch=patch)


def memory_tick(app: Any, now: datetime | None = None) -> None:
    """One scheduler tick of the background pass (call off the event loop)."""
    if not bool(setting("memory.background", True)):
        return
    now = now or datetime.now(UTC)
    collected = started = 0
    skipped: dict[str, int] = {}
    for session in list(app.state.sessions.list()):
        try:
            if (session.metadata or {}).get(IN_FLIGHT):
                collected += 1
                _collect(app, session)
            elif reason := skip_reason(app, session, now):
                skipped[reason] = skipped.get(reason, 0) + 1
            else:
                started += 1
                _start_pass(app, session)
        except ClioError as error:
            if error.error_type == "transcript_not_ready":  # still booting: next tick
                skipped["transcript_not_ready"] = skipped.get("transcript_not_ready", 0) + 1
                continue
            app.state.sessions.update(
                session.id,
                metadata_patch={
                    LAST_PASS: {
                        "status": "failed",
                        "reason": str(error.error_type),
                        "error": str(error)[:500],
                        "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                },
            )
            logger.warning("memory pass session=%s failed reason=%s", session.id, error.error_type)
    # What the last tick saw, for /v1/memory/summary: why nothing ran is answerable.
    app.state.memory_background = {
        "last_tick": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "in_flight": collected,
        "started": started,
        "skipped": skipped,
    }


def import_pasted_text(app: Any, text: str, *, scope: str, import_id: str) -> dict[str, Any]:
    """Assistant import: extract from pasted text (``source: imported``), then curate."""
    from clio_agent.gact.memory.extract import extract_candidates, pasted_items  # noqa: PLC0415
    from clio_agent.gact.memory.runtime import memory_service  # noqa: PLC0415
    from clio_agent.gact.memory.scope import check_scope  # noqa: PLC0415
    from clio_agent.gact.workspace_scope import GLOBAL_WORKSPACE_ID  # noqa: PLC0415

    target = check_scope(scope)
    service = memory_service(app)
    candidates, report = extract_candidates(
        pasted_items(text, import_id=import_id),
        session_id=import_id,
        forgotten=_forgotten_facts(service, (target,)),
        include_sensitive=bool(setting("memory.sensitive", False)),
        imported=True,
    )
    if not candidates:
        return {"import_id": import_id, "candidates": 0, "extract": report}
    workspace = target.split("/", 1)[1] if target.startswith("project/") else GLOBAL_WORKSPACE_ID
    run = start_curator(
        app,
        origin=f"import:{import_id}",
        source_session=None,
        workspace_id=workspace,
        scopes=(target,),
        candidates=[candidate.to_record() for candidate in candidates],
        run_key=import_id,
    )
    return {"import_id": import_id, "candidates": len(candidates), "extract": report, **run}


__all__ = [
    "IN_FLIGHT",
    "MIN_USER_MESSAGES",
    "WATERMARK",
    "eligible",
    "import_pasted_text",
    "memory_tick",
    "start_curator",
]
