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
CHECKED = "memory_checked"  # {count, at, reason}: the transcript verdict at that point
TRANSCRIPT_REASONS = frozenset({"last_pass_failed", "too_few_new_messages"})
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
    policy = session_policy(app, session)
    if not (policy.enabled and policy.may_write):
        return policy.reason or "memory_not_writable"
    if session.status not in ("idle", "error", "cancelled"):
        return "session_busy"
    runner = getattr(app.state, "turn_runner", None)
    if runner is not None and runner.busy(session.id):
        return "session_busy"
    last = _parse(str(session.last_interaction_at or session.updated_at or ""))
    idle = timedelta(minutes=int(setting("memory.idle_minutes", 30)))
    if last is None or now - last < idle:
        return "not_idle_yet"
    # The transcript is read only past the cheap checks, and once per message count:
    # an idle conversation is not made resident again on every tick.
    checked = metadata.get(CHECKED) or {}
    if checked.get("reason") and checked == _checked(session, checked["reason"]):
        return str(checked["reason"])
    return _transcript_reason(app, session)


def _checked(session: Any, reason: str) -> dict[str, Any]:
    """The transcript verdict, keyed by what changes when the conversation does."""
    metadata = session.metadata or {}
    last_pass = metadata.get(LAST_PASS) or {}
    minimum = _minimum_user_messages()
    return {
        "count": session.message_count,
        "at": str(session.last_interaction_at or ""),
        "reason": reason,
        "minimum_user_messages": minimum,
        "watermark": str(metadata.get(WATERMARK) or ""),
        "last_pass_status": str(last_pass.get("status") or ""),
        "last_pass_read_to": str(last_pass.get("read_to") or ""),
    }


def _transcript_reason(app: Any, session: Any) -> str:
    """The skip reasons that need the transcript (``""`` when a pass is due)."""
    metadata = session.metadata or {}
    messages = app.state.messages.get(session.id, [])
    failed = metadata.get(LAST_PASS) or {}
    if (
        failed.get("status", "completed") not in {"completed", "no_candidates"}
        and messages
        and failed.get("read_to") == messages[-1].id
    ):
        return "last_pass_failed"  # retried once the conversation has new messages
    fresh = _messages_after(messages, metadata.get(WATERMARK, ""))
    minimum = _minimum_user_messages()
    if sum(1 for message in fresh if message.role == "user") < minimum:
        return "too_few_new_messages"
    return ""


def _minimum_user_messages() -> int:
    """Minimum fresh user turns required by the configured scheduling mode."""
    return 1 if int(setting("memory.idle_minutes", 30)) == 0 else MIN_USER_MESSAGES


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
    allowed_scopes: tuple[str, ...],
    forgotten: list[str],
    rejected: list[str],
    pending: list[str],
    documents: list[dict[str, Any]],
) -> str:
    return (
        "Curate long-term memory from these candidates.\n"
        "Only use the exact allowed scopes supplied below. A memory document address is "
        "<scope>/<category>/<slug>, such as user/areas/field-notes or "
        "project/ws_123/areas/field-notes. For a project scope, include the workspace id "
        "as the second path component. Categories are profile, preferences, topics, areas, "
        "people, and environments. The singleton documents are <scope>/profile/profile "
        "and <scope>/preferences/preferences. Never use a scope itself as a document name.\n"
        "Use memory_search to find relevant existing documents. Read a listed document "
        "before changing it; when no relevant document exists, propose a new document "
        "within an allowed scope without trying to read the scope.\n\n"
        + json.dumps(
            {
                "allowed_scopes": list(allowed_scopes),
                "candidates": candidates,
                "forgotten": forgotten,
                "rejected": rejected,
                "pending": pending,
                "documents": documents,
            },
            indent=1,
        )
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
    from clio_agent.gact.memory.creation_runs import (  # noqa: PLC0415
        creation_run_id,
        update_run,
    )
    from clio_agent.gact.memory.models import memory_model_identity  # noqa: PLC0415
    from clio_agent.gact.memory.proposals import ProposalStore  # noqa: PLC0415
    from clio_agent.gact.memory.runtime import memory_service, memory_usage  # noqa: PLC0415
    from clio_agent.gact.memory.usage import document_subject  # noqa: PLC0415
    from clio_agent.gact.native_work import now  # noqa: PLC0415

    service = memory_service(app)
    usage = memory_usage(app)
    pin, agent_id = _curator_resolution(app)
    kept = ProposalStore(service).all()
    rejected = [f"{p.target}: {'; '.join(p.facts)}" for p in kept if p.status == "rejected"]
    # Already waiting for review: proposing them again only doubles the inbox.
    pending = [f"{p.target}: {'; '.join(p.facts)}" for p in kept if p.status == "pending"]
    expiry_days = int(setting("memory.expiry_days", 60))
    cutoff = (
        (datetime.now(UTC) - timedelta(days=expiry_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if expiry_days > 0
        else None
    )
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
        if cutoff and stored.name.category not in ("profile", "preferences") and last < cutoff:
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
        allowed_scopes=scopes,
        forgotten=_forgotten_facts(service, scopes),
        rejected=rejected,
        pending=pending,
        documents=documents,
    )
    human = str(app.state.human_principal)
    run_id = creation_run_id(origin, run_key)
    curator_model = memory_model_identity("memory_consolidate")
    metadata = {
        **pin,
        "session_type": "agent_task",
        SESSION_CURATOR: True,
        CANDIDATES: candidates,
        SCOPES: list(scopes),
        ORIGIN: origin,
        "model_role": "memory_consolidate",
        "memory_creation_run_id": run_id,
        "memory_creation_model": curator_model,
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
    update_run(
        service.store,
        run_id,
        {
            "state": "running",
            "reason": "",
            "curator_session": session.id,
            "workspace_id": workspace_id,
            "handle": handle,
            "models": {"curator": curator_model},
        },
    )
    return {"session": session.id, "handle": handle, "run_id": run_id}  # mail signals runner


def _start_pass(app: Any, session: Any) -> None:
    from clio_agent.gact.memory.creation_runs import (  # noqa: PLC0415
        begin_run,
        creation_run_id,
        known_zero_usage,
        record_extract,
        update_run,
    )
    from clio_agent.gact.memory.extract import extract_candidates, transcript_items  # noqa: PLC0415
    from clio_agent.gact.memory.runtime import memory_service  # noqa: PLC0415

    metadata = session.metadata or {}
    messages = list(app.state.messages.get(session.id, []))
    items = transcript_items(messages, after=str(metadata.get(WATERMARK, "")))
    last = messages[-1].id if messages else ""
    policy = session_policy(app, session)
    service = memory_service(app)
    origin = f"session:{session.id}"
    run_key = f"{session.id}:{last}"
    run_id = creation_run_id(origin, run_key)
    begin_run(
        service.store,
        run_id=run_id,
        origin=origin,
        source_session=session.id,
        workspace_id=session.workspace_id,
    )
    try:
        candidates, report = extract_candidates(
            items,
            session_id=session.id,
            forgotten=_forgotten_facts(service, policy.scopes),
            include_sensitive=bool(setting("memory.sensitive", False)),
        )
    except Exception as error:
        update_run(
            service.store,
            run_id,
            {
                "state": "failed",
                "reason": _failure_reason(error),
                "completed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        raise
    record_extract(service.store, run_id, report)
    current = app.state.sessions.get(session.id)
    runner = getattr(app.state, "turn_runner", None)
    latest = list(app.state.messages.get(session.id, []))
    if (
        current is None
        or (runner is not None and runner.busy(session.id))
        or current.status not in ("idle", "error", "cancelled")
        or len(latest) != len(messages)
        or (latest and latest[-1].id != last)
    ):
        update_run(
            service.store,
            run_id,
            {
                "state": "skipped",
                "reason": "conversation_changed_during_extract",
                "completed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "_curator_usage": known_zero_usage(),
            },
        )
        return
    logger.info("memory pass session=%s extract=%s", session.id, report)
    if not candidates:  # nothing worth keeping: the pass is done, and says so
        empty = {"read_to": last, "extract": report}
        patch = collect_patch(empty, "no_candidates", datetime.now(UTC))
        app.state.sessions.update(session.id, metadata_patch={**patch, WATERMARK: last})
        update_run(
            service.store,
            run_id,
            {
                "state": "completed",
                "reason": "no_candidates",
                "completed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "_curator_usage": known_zero_usage(),
            },
        )
        return
    try:
        run = start_curator(
            app,
            origin=origin,
            source_session=session,
            workspace_id=session.workspace_id,
            scopes=policy.scopes,
            candidates=[candidate.to_record() for candidate in candidates],
            run_key=run_key,
        )
    except Exception as error:
        update_run(
            service.store,
            run_id,
            {
                "state": "failed",
                "reason": _failure_reason(error),
                "completed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        raise
    app.state.sessions.update(
        session.id,
        metadata_patch={
            IN_FLIGHT: {
                **run,
                "run_id": run_id,
                "origin": origin,
                "read_to": last,
                "extract": report,
            }
        },
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
    from clio_agent.gact.memory.creation_runs import (  # noqa: PLC0415
        curator_run_outcome,
        failed_memory_tool_count,
        proposal_summary,
        update_run,
    )
    from clio_agent.gact.memory.runtime import memory_service  # noqa: PLC0415

    run = (session.metadata or {}).get(IN_FLIGHT) or {}
    task = app.state.task_history.task_snapshot(str(run.get("handle", "")))
    if task is not None and task.terminal_at is None:
        return
    status = task.status if task is not None else "missing"
    run_id = str(run.get("run_id") or "")
    service = memory_service(app) if run_id else None
    counts, targets = (
        proposal_summary(service, str(run.get("origin") or ""), str(run.get("handle") or ""))
        if service is not None
        else ({"accepted": 0, "pending": 0, "rejected": 0}, [])
    )
    curator_messages = app.state.messages.get(str(run.get("session") or ""), [])
    status, run_reason = curator_run_outcome(
        status, counts, failed_memory_tool_count(curator_messages)
    )
    patch = collect_patch(run, status, datetime.now(UTC))
    if WATERMARK not in patch:
        logger.warning(
            "memory pass session=%s curator=%s ended status=%s; its messages stay unread",
            session.id,
            run.get("session", ""),
            status,
        )
    app.state.sessions.update(session.id, metadata_patch=patch)
    if run_id:
        assert service is not None
        update_run(
            service.store,
            run_id,
            {
                "state": status,
                "reason": run_reason,
                "completed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "proposal_counts": counts,
                "targets": targets,
                "_curator_usage": _curator_usage(app.state.sessions.get(run.get("session", ""))),
            },
        )


def memory_tick(app: Any, now: datetime | None = None) -> None:
    """One scheduler tick of the background pass (call off the event loop)."""
    now = now or datetime.now(UTC)
    if not bool(setting("memory.background", True)):
        app.state.memory_background = {
            "last_tick": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "off": True,  # "Generate memory from sessions" is off: no pass runs
        }
        return
    from clio_agent.gact.memory.models import (  # noqa: PLC0415
        MEMORY_MODEL_UNCONFIGURED,
        unconfigured_roles,
    )

    if missing := unconfigured_roles():
        # No model to run on: the pass does not run (no probe of an unselected default).
        previous = getattr(app.state, "memory_background", None) or {}
        if previous.get("skipped_reason") != MEMORY_MODEL_UNCONFIGURED:  # once, not per tick
            logger.warning(
                "memory pass skipped reason=%s roles=%s",
                MEMORY_MODEL_UNCONFIGURED,
                ",".join(missing),
            )
        app.state.memory_background = {
            "last_tick": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "skipped_reason": MEMORY_MODEL_UNCONFIGURED,
            "roles": list(missing),
        }
        return
    collected = started = 0
    skipped: dict[str, int] = {}
    for session in list(app.state.sessions.list()):
        try:
            if (session.metadata or {}).get(IN_FLIGHT):
                collected += 1
                _collect(app, session)
            elif reason := skip_reason(app, session, now):
                skipped[reason] = skipped.get(reason, 0) + 1
                checked = _checked(session, reason)
                if (
                    reason in TRANSCRIPT_REASONS
                    and (session.metadata or {}).get(CHECKED) != checked
                ):
                    app.state.sessions.update(session.id, metadata_patch={CHECKED: checked})
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
    from clio_agent.gact.memory.creation_runs import (  # noqa: PLC0415
        begin_run,
        creation_run_id,
        known_zero_usage,
        record_extract,
        update_run,
    )
    from clio_agent.gact.memory.extract import extract_candidates, pasted_items  # noqa: PLC0415
    from clio_agent.gact.memory.runtime import memory_service  # noqa: PLC0415
    from clio_agent.gact.memory.scope import check_scope  # noqa: PLC0415
    from clio_agent.gact.workspace_scope import GLOBAL_WORKSPACE_ID  # noqa: PLC0415

    target = check_scope(scope)
    service = memory_service(app)
    workspace = target.split("/", 1)[1] if target.startswith("project/") else GLOBAL_WORKSPACE_ID
    origin = f"import:{import_id}"
    run_id = creation_run_id(origin, import_id)
    begin_run(service.store, run_id=run_id, origin=origin, workspace_id=workspace)
    try:
        candidates, report = extract_candidates(
            pasted_items(text, import_id=import_id),
            session_id=import_id,
            forgotten=_forgotten_facts(service, (target,)),
            include_sensitive=bool(setting("memory.sensitive", False)),
            imported=True,
        )
    except Exception as error:
        update_run(
            service.store,
            run_id,
            {
                "state": "failed",
                "reason": _failure_reason(error),
                "completed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        raise
    record_extract(service.store, run_id, report)
    if not candidates:
        update_run(
            service.store,
            run_id,
            {
                "state": "completed",
                "reason": "no_candidates",
                "completed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "_curator_usage": known_zero_usage(),
            },
        )
        return {"import_id": import_id, "candidates": 0, "extract": report, "run_id": run_id}
    try:
        run = start_curator(
            app,
            origin=origin,
            source_session=None,
            workspace_id=workspace,
            scopes=(target,),
            candidates=[candidate.to_record() for candidate in candidates],
            run_key=import_id,
        )
    except Exception as error:
        update_run(
            service.store,
            run_id,
            {
                "state": "failed",
                "reason": _failure_reason(error),
                "completed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        raise
    return {"import_id": import_id, "candidates": len(candidates), "extract": report, **run}


def _failure_reason(error: Exception) -> str:
    """Expose a typed failure code or exception class, never transcript/error text."""
    return str(getattr(error, "error_type", "") or type(error).__name__)


def _curator_usage(session: Any) -> dict[str, Any]:
    """The curator's existing session rollup; preserve unknown cost as unknown."""
    if session is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "input_known": False,
            "output_known": False,
            "cost_usd": None,
            "cost_known": False,
        }
    known = bool(getattr(session, "cost_known", False))
    by_model = getattr(session, "usage_by_model", None) or {}
    tokens_known = bool(by_model) or bool(
        getattr(session, "tokens_input", 0) or getattr(session, "tokens_output", 0)
    )
    return {
        "input_tokens": max(0, int(getattr(session, "tokens_input", 0) or 0))
        if tokens_known
        else None,
        "output_tokens": max(0, int(getattr(session, "tokens_output", 0) or 0))
        if tokens_known
        else None,
        "input_known": tokens_known,
        "output_known": tokens_known,
        "cost_usd": float(getattr(session, "cost_usd", 0) or 0) if known else None,
        "cost_known": known,
    }


__all__ = [
    "IN_FLIGHT",
    "MIN_USER_MESSAGES",
    "WATERMARK",
    "eligible",
    "import_pasted_text",
    "memory_tick",
    "start_curator",
]
