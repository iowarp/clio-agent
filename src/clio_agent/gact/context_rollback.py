"""The agent's context follows an undo / rewind of the conversation.

The ledger is the UI's record; the agent's context is clio-core's working set per
agent scope (:mod:`clio_agent.gact.agents.clio_react_record`), recorded per turn. When
turns are rolled back in the ledger, each scope's live set is rebuilt as it stood
before the first rolled-back turn -- recorded ops (delete, then re-append), so what
a rolled-back compaction replaced comes back and nothing is erased from history. A
kept question of a rolled-back turn (a rewind that keeps the target user message)
stays as its user message.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

__all__ = ["follow_rollback", "roll_back_agent_context", "rolled_back_turns"]


def follow_rollback(app: Any, session: str, deleted: list[Any], kept: list[Any]) -> None:
    """Roll the session's agent context back with the ledger (undo / rewind)."""
    rolled, kept_users = rolled_back_turns(deleted, kept)
    arc = getattr(app.state, "arc", None)
    roll_back_agent_context(arc, session, rolled_back=rolled, kept_user_turns=kept_users)


def rolled_back_turns(deleted: Iterable[Any], kept: Iterable[Any]) -> tuple[set[str], set[str]]:
    """``(rolled-back turn ids, kept user-message turn ids)`` from ledger messages."""
    rolled = {str(getattr(m, "turn_id", "") or m.id) for m in deleted}
    kept_users = {str(getattr(m, "turn_id", "") or m.id) for m in kept if m.role == "user"}
    return rolled, kept_users & rolled


def roll_back_agent_context(
    arc: Any, session: str, *, rolled_back: set[str], kept_user_turns: set[str]
) -> None:
    """Rebuild every agent scope of ``session`` as it was before ``rolled_back``.

    Raises whatever the store raises: a context that silently kept rolled-back turns
    would show the model a conversation the user undid.
    """
    if arc is None or not rolled_back:
        return
    for scope in arc.list_segment_scopes(session):
        if scope.startswith("_"):
            continue
        history = arc.list_segments(session, scope, include_tombstoned=True)
        affected = [s for s in history if s.turn_id in rolled_back]
        if not affected:
            continue
        cut = min(s.logical_time for s in affected) - 1
        restored = list(arc.render_working_set(session, scope, as_of=cut))
        kept = [s for s in affected if _is_kept_question(s, kept_user_turns)]
        live = list(arc.render_working_set(session, scope))
        restored_ids = [s.id for s in restored]
        if [s.id for s in live[: len(restored)]] == restored_ids:
            # The common case: only the tail goes (the prefix the provider holds stays).
            tail = live[len(restored) :]
            keep_ids = {s.id for s in kept}
            doomed = [s.id for s in tail if s.id not in keep_ids]
            if doomed:
                arc.delete_segments(session, scope, doomed)
            continue
        arc.delete_segments(session, scope, [s.id for s in live])
        for seg in [*restored, *kept]:
            arc.append_segment(
                session,
                scope,
                seg.kind,
                seg.content,
                step=seg.step,
                token_count=seg.token_count,
                turn_id=seg.turn_id,
                expert_span_id=seg.expert_span_id,
                run_span_id=seg.run_span_id,
            )


def _is_kept_question(seg: Any, kept_user_turns: set[str]) -> bool:
    content = seg.content if isinstance(seg.content, dict) else {}
    return (
        seg.kind == "user"
        and seg.turn_id in kept_user_turns
        and content.get("actor", "user") == "user"
        and content.get("source") != "steer"
    )
