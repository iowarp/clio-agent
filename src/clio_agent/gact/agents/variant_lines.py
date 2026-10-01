"""One conversation line for a ``dspy.BestOfN`` / ``Refine`` agent, across turns.

The tries of a variant call run on their own run-keyed scopes (``agent#runN``) so they
never see each other. Across turns that alone would let try N continue its *own* line
from the previous call, a line the user never saw. Instead:

* **fork** -- as a try starts, its scope's previous line is retired (a recorded delete)
  and the base scope's working set is copied in, so every try starts from the
  conversation as it stands;
* **winner** -- once the engine selects, the winning try's new segments are appended to
  the base scope, so the next turn continues from the answer the user got. A winner
  that compacted what it forked hands the base its whole working set (the base's live
  segments are retired, then the winner's are appended).

``draft_alternatives`` asks for tries in the middle of a turn: its tries fork the
conversation before that turn's own message (``cut_id``), and the winner's line
replaces that turn on the base. A Refine try the user commented on forks from the
pick's scope instead (``source``), so it sees its predecessor and the advice.

Both are ordinary plane ops (appends and deletes), recorded like any other.
"""

from __future__ import annotations

import logging
from typing import Any

from clio_agent.errors import ClioError
from clio_agent.gact import context as _ctx

logger = logging.getLogger(__name__)


class VariantLineError(ClioError):
    """A try could not be forked or committed: the line it names is not live."""

    reason = "variant_line_unavailable"

    def __init__(self, detail: str) -> None:
        super().__init__(f"variant line unavailable: {detail}", error_type=self.reason)


def _plane() -> tuple[Any, str, str]:
    """``(plane, session, base_scope)`` for the active agent: its clio-core ARC, or the
    History mode plane; no plane is :class:`NoContextStoreError`."""
    from clio_agent.arc.history_plane import plane_for  # noqa: PLC0415
    from clio_agent.gact.agents.clio_react import NoContextStoreError  # noqa: PLC0415

    app = _ctx.active_app()
    base = _ctx.active_react_scope()
    arc = plane_for(app) if (app is not None and base) else None
    if arc is None:
        raise NoContextStoreError()
    return arc, _ctx.active_react_session(), base


def _run_scope(base: str, run_index: int) -> str:
    token = _ctx.set_react_run(run_index)
    try:
        return _ctx.run_keyed_scope(base)
    finally:
        _ctx.reset(token)


def _copy(arc: Any, session: str, scope: str, segments: list[Any]) -> list[str]:
    ids: list[str] = []
    for seg in segments:
        copied = arc.append_segment(
            session,
            scope,
            seg.kind,
            dict(seg.content),
            step=seg.step,
            token_count=seg.token_count,
            turn_id=seg.turn_id,
            expert_span_id=seg.expert_span_id,
            run_span_id=seg.run_span_id,
        )
        ids.append(copied.id)
    return ids


def _retire_live(arc: Any, session: str, scope: str) -> None:
    live = [seg.id for seg in arc.render_working_set(session, scope)]
    if live:
        arc.delete_segments(session, scope, live)


def fork_try(run_index: int, *, source: str = "", cut_id: str = "") -> list[str]:
    """Start try ``run_index`` from ``source``'s working set (default: the base scope).

    ``cut_id``: fork only what precedes that segment (``draft_alternatives`` forks the
    conversation before the turn that asked for the drafts). Returns the forked ids.
    """
    arc, session, base = _plane()
    scope = _run_scope(base, run_index)
    _retire_live(arc, session, scope)
    segments = list(arc.render_working_set(session, source or base))
    if cut_id:
        segments = segments[: _position(segments, cut_id, source or base)]
    return _copy(arc, session, scope, segments)


def try_scope(run_index: int) -> str:
    """The run-keyed scope try ``run_index`` of the active agent runs on."""
    _arc, _session, base = _plane()
    return _run_scope(base, run_index)


def _position(segments: list[Any], segment_id: str, scope: str) -> int:
    ids = [seg.id for seg in segments]
    if segment_id not in ids:
        raise VariantLineError(f"segment {segment_id!r} is not live on scope {scope!r}")
    return ids.index(segment_id)


def try_steps(run_index: int) -> list[Any]:
    """Try ``run_index``'s context as clio-core holds it (what it saw and did)."""
    from clio_agent.gact.agents.clio_react_record import read_steps  # noqa: PLC0415

    arc, session, base = _plane()
    return read_steps(arc, session, _run_scope(base, run_index))


def record_advice(run_index: int, advice: str, source: str) -> None:
    """Record advice on try ``run_index``'s own scope, before it runs: a CLIO addition the
    model is told and the UI shows."""
    from clio_agent.gact.injection_parts import emit_injection  # noqa: PLC0415

    arc, session, base = _plane()
    note = {"text": advice, "source": source, "actor": "algorithm"}
    arc.append_segment(session, _run_scope(base, run_index), "user", note, step=0)
    emit_injection(source, advice, agent_id=base)


def record_winner(run_index: int, forked: list[str], *, cut_id: str = "") -> None:
    """Continue the base scope with the winning try's line.

    ``cut_id``: that segment and the rest of its turn on the base (the turn that asked
    for drafts) are retired first, so the conversation continues from the winner's line.
    """
    arc, session, base = _plane()
    live = list(arc.render_working_set(session, _run_scope(base, run_index)))
    if [seg.id for seg in live[: len(forked)]] == forked:
        new = live[len(forked) :]
        if cut_id:
            base_live = list(arc.render_working_set(session, base))
            tail = base_live[_position(base_live, cut_id, base) :]
            doomed = [seg.id for seg in tail if seg.turn_id == tail[0].turn_id]
            arc.delete_segments(session, base, doomed)
    else:
        logger.info(
            "variant.winner.compacted agent=%s run_index=%d: base takes its working set",
            base,
            run_index,
        )
        _retire_live(arc, session, base)
        new = live
    _copy(arc, session, base, new)
