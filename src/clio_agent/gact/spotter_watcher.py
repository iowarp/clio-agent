"""SPOTTER producers over Core mail and ordinary conversation assignments.

Arming creates an idle child assignment. Activity persists a coalesced factual
mail item; TurnRunner alone decides whether to inject or admit execution.
Conversation observations supply status, failure and physical retirement.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from clio_agent.arc.task_types import CoordinationGrant, MailItem, TaskSnapshot
from clio_agent.gact.fleet.spotter_model import model_refusal, watcher_model_ref
from clio_agent.gact.native_work import now
from clio_agent.gact.spotter_clearance import release_clearance_event, signal_clearance

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import Session

logger = logging.getLogger(__name__)
SPOTTER_APPROVAL_MODE = "spotter-ai"
# gact-tui recognizes the watcher card by this title prefix (subagent-card.tsx); change both together.
WATCHER_RUN_LABEL = "SPOTTER AI"
_WAKE_TURN_FINAL_TEXT = "Watched session activity: the session's turn completed. The provenance store may have new entries."
_WAKE_COALESCED_TEXT = "Watched session activity: multiple tool calls completed. The provenance store may have new entries."


def _structural_item_identity(value: Any) -> str:
    """Return a bounded identity copied from one typed result item.

    The wake lane must distinguish successive equal-sized batches without
    authoring a semantic summary. Prefer explicit identifier fields on
    mapping-shaped items; scalar list entries are already their own identity.
    """

    if isinstance(value, Mapping):
        for key in ("run_id", "task_id", "artifact_id", "verdict_id", "id"):
            candidate = str(value.get(key) or "").strip()
            if candidate:
                return candidate[:80]
        return ""
    if isinstance(value, str):
        return str(value).strip()[:80]
    return ""


def _tool_outcome_fact(structured_result: Any) -> str:
    """A short, STRUCTURAL fact drawn from a completed tool's own typed result
    (#1218 e) — never an authored/inferred summary. When the result is a
    mapping, the first list-valued entry's length is reported using that
    entry's OWN key as the noun (e.g. ``{"verdicts": [...]*5}`` -> ``"5
    verdicts reported"``), so the fact is always traceable to real wire data.
    When the items expose structural identifiers, the first/last identifiers
    are included. This keeps equal-sized successive batches causally distinct
    without re-narrating or inferring anything from their contents.
    Returns ``""`` when the result carries no list-shaped evidence (a tool
    that returns scalars/None yields no fabricated count — never a guess).
    """

    if not isinstance(structured_result, Mapping):
        return ""
    for key, value in structured_result.items():
        if isinstance(value, (list, tuple)):
            noun = str(key).replace("_", " ").strip() or "items"
            fact = f"{len(value)} {noun} reported"
            identities = [
                identity for item in value if (identity := _structural_item_identity(item))
            ]
            if identities:
                if len(identities) == 1 or identities[0] == identities[-1]:
                    return f"{fact}: {identities[0]}"
                return f"{fact}: {identities[0]} through {identities[-1]}"
            return fact
    return ""


def _wake_text(*, tool_name: str = "", tool_ok: bool = True, tool_result: Any = None) -> str:
    """Compose the fact-carrying wake payload for ONE (non-coalesced) push
    (#1218 e). ``tool_name`` empty means the PARENT's own turn just finished
    (:func:`on_turn_finalized`'s branch) — otherwise it names the tool that
    just completed on the protected parent session, plus (when derivable) a
    structural fact about what it reported. FACTS ONLY: no directive verbs —
    the watcher's own prompt (not this payload) owns its reasoning about what
    to do next.
    """

    if not tool_name:
        return _WAKE_TURN_FINAL_TEXT
    if not tool_ok:
        return (
            f"Watched session activity: {tool_name} failed. "
            "The provenance store may have new entries."
        )
    fact = _tool_outcome_fact(tool_result)
    suffix = f" ({fact})" if fact else ""
    return (
        f"Watched session activity: {tool_name} completed{suffix}. "
        "The provenance store may have new entries."
    )


def _watcher_agent_ref() -> str:
    """The agent the watcher child runs (config: ``spotter.watcher_agent``)."""

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "spotter.watcher_agent",
        env="CLIO_SPOTTER_AGENT",
        default="clio/spotter-ai",
        cast=conf.as_str,
    )


def sync_watcher_for_mode(
    app: "FastAPI", session: "Session", *, prior_approval_mode: str = ""
) -> None:
    """Route hook: arm/disarm the watcher off ``session``'s PERSISTED TRANSITION.

    The single call site both ``create_session`` and ``patch_session``
    (``gact/routes/sessions.py``) use so neither route has to inline the
    arm-vs-disarm branch itself.

    Arms ONLY on a genuine transition INTO spotter-ai (``prior_approval_mode``
    differs from the current mode — a fresh create has no prior mode, so it
    always qualifies). Disarms ONLY on a genuine transition AWAY from it. An
    unrelated PATCH (mode unchanged, spotter-ai before and after — e.g. a
    title rename, a pin toggle) is a true no-op: neither branch runs.

    An arming transition is GATED first (:mod:`clio_agent.gact.spotter_arming`,
    which carries the full case): the watcher agent's plugin MCP servers
    must statically resolve. When they do not, the transition is REFUSED with a
    typed 422 instead of arming a watcher that cannot execute — whose only
    downstream signal would be the clearance barrier denying every mutating tool
    call. A transition AWAY from spotter-ai is never gated: disarm must always
    be possible, including on a deployment that broke after arming.

    Args:
        app: The GACT app.
        session: The just-created-or-patched session, already persisted.
        prior_approval_mode: The session's approval_mode BEFORE this call (empty
            for a fresh create, where there is no prior mode).

    Raises:
        fastapi.HTTPException: 422 when the session transitions INTO spotter-ai
            but its watcher could not execute. The persisted transition is
            rolled back first (a refused create deletes the just-minted row; a
            refused patch restores the prior mode), so a refusal never leaves a
            half-armed session behind.
    """

    entered_spotter_ai = (
        session.approval_mode == SPOTTER_APPROVAL_MODE
        and prior_approval_mode != SPOTTER_APPROVAL_MODE
    )
    left_spotter_ai = (
        prior_approval_mode == SPOTTER_APPROVAL_MODE
        and session.approval_mode != SPOTTER_APPROVAL_MODE
    )
    if entered_spotter_ai:
        from clio_agent.gact.spotter_arming import (  # noqa: PLC0415
            refuse_watcher_arming,
            validate_watcher_arming,
        )

        refusal = validate_watcher_arming(app, session_id=session.id) or model_refusal(app, session)
        if refusal is not None:
            refuse_watcher_arming(app, session, refusal, prior_approval_mode=prior_approval_mode)
        task = ensure_spotter_watcher(app, session)
        if task is not None:
            from clio_agent.gact.spotter_arm_review import wake_on_arm  # noqa: PLC0415

            wake_on_arm(app, session, task)
    elif left_spotter_ai:
        disarm_spotter_watcher(app, session)


def _live_watcher_tasks(app: "FastAPI", parent_session_id: str) -> list[TaskSnapshot]:
    """Resolve armed watcher addresses from Core's resource index."""
    history = getattr(app.state, "task_history", None)
    if history is None:
        return []
    sessions = {
        task.owner.session_id
        for handle in history.resource_handles("spotter:" + parent_session_id)
        if (task := history.task_snapshot(handle)) is not None and task.kind == "Subagent"
    }
    candidates = [
        task
        for sid in sorted(sessions)
        if (task := history.latest_assignment(sid)) is not None
        and task.cancellation_requested_at is None
        and task.status != "cancelled"
    ]
    live = []
    for task in candidates:
        try:
            app.state.conversations.require_admission(task)
        except ValueError:
            continue  # A cancellation hold closes admission before its Core write completes.
        live.append(task)
    return live


def _watcher_read_grant_id(task: TaskSnapshot) -> str:
    """The configuration grant identity for one armed watcher assignment."""
    return "spotter-read:" + task.handle


def _grant_watcher_read(app: "FastAPI", parent_session_id: str, task: TaskSnapshot) -> None:
    """Let one armed watcher read/observe its protected parent (idempotent, off-loop).

    Arming is a human configuration act, so it may convey the content rights a
    delegation-reply grant deliberately withholds; the grant is scoped to the
    parent session and to this exact watcher handle's lifetime (disarm revokes
    it). An existing grant with the same identity — live or revoked — or any
    live SPOTTER read grant on the same parent is kept and nothing is written.
    """
    history = app.state.task_history
    grant_id = _watcher_read_grant_id(task)
    with history.serialized(writing=True):
        if any(
            grant.id == grant_id
            or (
                grant.id.startswith("spotter-read:")
                and grant.target_session == parent_session_id
                and grant.revoked_at is None
            )
            for grant in history.coordination_grants(task.owner.session_id)
        ):
            return
        history.record_grant(
            CoordinationGrant(
                id=grant_id,
                granting_human=task.owner.human_id,
                recipient_session=task.owner.session_id,
                target_session=parent_session_id,
                rights=("read", "observe"),
                created_at=now(),
            ),
            observation_id=uuid4().hex,
        )


def _revoke_watcher_read(app: "FastAPI", watcher_session_id: str, parent_session_id: str) -> None:
    """Revoke every live SPOTTER read grant a watcher session holds on its parent.

    Each grant is re-recorded with ``revoked_at`` (its identity, rights and
    target unchanged), so the revocation is a durable historical observation.
    """
    history = app.state.task_history
    with history.serialized(writing=True):
        for grant in history.coordination_grants(watcher_session_id):
            if (
                grant.id.startswith("spotter-read:")
                and grant.target_session == parent_session_id
                and grant.revoked_at is None
            ):
                history.record_grant(
                    grant.model_copy(update={"revoked_at": now()}),
                    observation_id=uuid4().hex,
                )


def ensure_spotter_watcher(app: "FastAPI", session: "Session") -> TaskSnapshot | None:
    """Arm one idle watcher through Conversations, without launching a check."""
    if session.approval_mode != SPOTTER_APPROVAL_MODE:
        return None
    history, owner = app.state.task_history, app.state.conversations
    with history.serialized():
        existing = _live_watcher_tasks(app, session.id)
        if existing:
            _grant_watcher_read(app, session.id, existing[0])  # backfill pre-grant arms
            return existing[0]
        parent = history.latest_assignment(session.id)
        if parent is None or parent.terminal_at is not None:
            parent = owner._new_assignment(
                session.id,
                "Work in this session with SPOTTER review enabled.",
                owner.caller(session.id).human_id,
                predecessor=parent,
                invocation_id="spotter-arm:" + uuid4().hex,
            )
        # A re-arm after a disarm (or a changed watcher model) is a new invocation,
        # never the cancelled watcher's identity.
        armed = len(history.resource_handles("spotter:" + session.id))
        invocation = "spotter:" + parent.handle + (f":{armed}" if armed else "")
    # Route callers run this finite adapter off the application loop.
    receipt = asyncio.run_coroutine_threadsafe(
        owner.spawn(
            owner.caller(session.id),
            "Observe the owning session when workload evidence arrives and raise factual review findings.",
            _watcher_agent_ref(),
            invocation,
            resource_id="spotter:" + session.id,
            session_metadata={"spotter_parent_session": session.id},
            approval_profile="spotter-watcher",
            start=False,
            standing=True,
            model=watcher_model_ref(app, session),
        ),
        owner._loop,
    ).result()
    task = history.task_snapshot(receipt["handle"])
    if task is None:
        raise RuntimeError("Watcher acceptance was not recorded")
    _grant_watcher_read(app, session.id, task)
    app.state.sessions.update(task.owner.session_id, title=WATCHER_RUN_LABEL)
    signal_clearance(app, session.id)
    return task


def _push_wake(app: "FastAPI", parent_id: str, task: TaskSnapshot, text: str) -> None:
    """Persist factual input; coalesce only not-yet-committed watcher activity."""
    history = app.state.task_history
    with history.serialized():
        app.state.conversations.require_admission(task)
        pending = next(
            (
                item
                for item in history.pending_mail(task.owner.session_id)
                if item.kind == "spotter" and item.sender_session == parent_id
            ),
            None,
        )
        content = history.write_content(
            task.owner.session_id, {"text": _WAKE_COALESCED_TEXT if pending is not None else text}
        )
        if pending is None:
            history.record_mail(
                MailItem(
                    id="spotter:" + uuid4().hex,
                    recipient_session=task.owner.session_id,
                    sender_session=parent_id,
                    kind="spotter",
                    content=content,
                    created_at=now(),
                    handle=task.handle,
                )
            )
        else:
            history.update_mail(
                pending.model_copy(update={"content": content, "revision": pending.revision + 1}),
                expected_revision=pending.revision,
            )
    signal_clearance(app, parent_id)


def disarm_spotter_watcher(app: "FastAPI", session: "Session") -> None:
    """Request targeted cancellation from the real conversation owner."""
    owner = app.state.conversations
    for task in _live_watcher_tasks(app, session.id):
        _revoke_watcher_read(app, task.owner.session_id, session.id)
        asyncio.run_coroutine_threadsafe(
            owner.cancel(owner.caller(session.id), task.handle), owner._loop
        ).result()
    release_clearance_event(app, session.id)


def wake_on_parent_activity(
    app: "FastAPI",
    parent_session_id: str,
    *,
    tool_name: str = "",
    ok: bool = True,
    result: Any = None,
) -> None:
    """Publish parent activity without introducing another admission policy."""
    session = app.state.sessions.get(parent_session_id)
    if session is None or session.approval_mode != SPOTTER_APPROVAL_MODE:
        return
    tasks = _live_watcher_tasks(app, parent_session_id)
    if tasks:
        _push_wake(
            app,
            parent_session_id,
            tasks[0],
            _wake_text(tool_name=tool_name, tool_ok=ok, tool_result=result),
        )


#: Bound on the answer excerpt a turn-completed wake carries.
_WAKE_ANSWER_CHARS = 600


def _latest_answer_fact(app: "FastAPI", session_id: str) -> str:
    """The turn's final answer as a wake fact: its message id and a bounded excerpt.

    Read only from the resident transcript (never a store read on this path); empty
    when the answer is not resident or has no text.
    """
    messages = app.state.messages
    get_if_resident = getattr(messages, "get_if_resident", None)
    rows = (
        get_if_resident(session_id)
        if callable(get_if_resident)
        else messages.get(session_id)
        if isinstance(messages, dict)
        else None
    ) or []
    for message in reversed(list(rows)):
        if getattr(message, "role", "") != "assistant":
            continue
        text = " ".join(
            str(getattr(part, "text", "") or "")
            for part in getattr(message, "parts", [])
            if getattr(part, "type", "") == "text"
        ).strip()
        if not text:
            return ""
        excerpt = " ".join(text.split())
        if len(excerpt) > _WAKE_ANSWER_CHARS:
            excerpt = "..." + excerpt[-_WAKE_ANSWER_CHARS:]
        return f"Its answer is message {message.id}: {excerpt}"
    return ""


def on_turn_finalized(app: "FastAPI", session_id: str) -> None:
    """Publish parent evidence; child settlement remains conversation-owned."""
    session = app.state.sessions.get(session_id)
    if session is None:
        return
    if session.approval_mode == SPOTTER_APPROVAL_MODE:
        tasks = _live_watcher_tasks(app, session_id)
        if tasks:
            # The new answer itself, so a review starts from what changed, not memory.
            fact = _latest_answer_fact(app, session_id)
            text = f"{_WAKE_TURN_FINAL_TEXT} {fact}" if fact else _WAKE_TURN_FINAL_TEXT
            _push_wake(app, session_id, tasks[0], text)
    elif session.metadata.get("spotter_parent_session"):
        signal_clearance(app, str(session.metadata["spotter_parent_session"]))


def on_turn_runner_idle(app: "FastAPI", session_id: str) -> None:
    """Signal clearance only after the actual session worker has retired."""
    session = app.state.sessions.get(session_id)
    if session is not None and session.metadata.get("spotter_parent_session"):
        signal_clearance(app, str(session.metadata["spotter_parent_session"]))
