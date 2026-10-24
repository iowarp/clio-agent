"""``Answer`` hooks: see the turn's final answer and act on it before the turn is published.

Fires once per turn at finalize, after the answer channel settled and before the
transcript freezes. The answer may already have streamed, so nothing here is a gate
and nothing changes silently:

* a hook's ``note`` lands as a ``notice`` part under the answer, attributed to it;
* a hook's ``check`` (``passed`` + ``text``) lands as a passed or failed ``notice``;
* a replacement answer lands as a NEW answer part marked as a revision by that hook,
  after a ``revised`` notice naming the hook, its reason and the part it revises; the
  original part is annotated ``superseded_by`` (its text is never rewritten: it was
  sealed and persisted when it closed), so a client can fold it behind the notice.

A hook infrastructure failure is recorded by the dispatcher on its record and never
fails the turn; the outcome is emitted as a ``hook.answer`` semantic event.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from clio_agent.gact.hooks.dispatcher import dispatch_answer, get_global_dispatcher
from clio_agent.gact.hooks.events import ANSWER
from clio_agent.gact.parts import Part

if TYPE_CHECKING:
    from clio_agent.gact.turn_state import TurnState

logger = logging.getLogger(__name__)

#: ``notice`` source for every part an Answer hook adds.
ANSWER_HOOK_SOURCE = "answer_hook"


def _notice(agent_id: str, hook_id: str, code: str, text: str, **metadata: str) -> Part:
    return Part(
        id=f"part_answer_hook_{uuid.uuid4().hex[:10]}",
        type="notice",
        source=ANSWER_HOOK_SOURCE,
        code=code,
        agent_id=agent_id,
        text=text,
        metadata={"hook": hook_id, **metadata},
    )


def apply_answer_hooks(
    state: "TurnState", *, answer_text: str, answer_part_id: str, agent_id: str
) -> None:
    """Fire ``Answer`` hooks on the settled answer and land what they return."""

    if not answer_text.strip() or state.error_info is not None:
        return
    dispatcher = get_global_dispatcher()
    if dispatcher is None:
        return
    agent_ref = ""
    if state.dynamic_agent_used is not None:
        agent_ref = str(state.dynamic_agent_used.metadata.get("agent_ref") or "")
    outcome = dispatch_answer(
        {"answer": answer_text, "agent_id": agent_id, "agent_ref": agent_ref},
        session_id=state.sid,
        turn_id=state.turn_id,
        cwd=str(getattr(state.sess, "workspace_root", "") or ""),
    )
    if not outcome.records:
        return
    transcript = state.transcript
    revised = outcome.updated_output.strip()
    if revised and revised != answer_text.strip():
        hook_id = outcome.updated_output_hook
        transcript.append_part(
            _notice(agent_id, hook_id, "revised", outcome.reason, revises=answer_part_id),
            stream_source="batch",
        )
        revision = Part(
            id=f"part_answer_revision_{uuid.uuid4().hex[:10]}",
            type="text",
            agent_id=agent_id,
            text=outcome.updated_output,
            metadata={"revises": answer_part_id, "revised_by": hook_id, "reason": outcome.reason},
        )
        landed = transcript.append_part(revision, stream_source="batch")
        if landed is not None and answer_part_id:
            transcript.annotate(answer_part_id, superseded_by=landed.id, superseded_by_hook=hook_id)
        state.answer_text = outcome.updated_output
    for note in outcome.notes:
        transcript.append_part(
            _notice(agent_id, note["hook_id"], "note", note["text"]), stream_source="batch"
        )
    for check in outcome.checks:
        code = "check_passed" if check["passed"] else "check_failed"
        transcript.append_part(
            _notice(agent_id, check["hook_id"], code, check["text"]), stream_source="batch"
        )
    _emit(state, outcome.records, revised=bool(revised), notes=len(outcome.notes))


def _emit(state: "TurnState", records: list[dict[str, Any]], *, revised: bool, notes: int) -> None:
    from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

    _emit_semantic_event(
        state.app,
        state.sid,
        "hook.answer",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        status="completed",
        summary=f"{len(records)} answer hook(s) ran.",
        actor={"hook": ANSWER},
        payload={"records": records, "revised": revised, "notes": notes},
    )


__all__ = ["ANSWER_HOOK_SOURCE", "apply_answer_hooks"]
