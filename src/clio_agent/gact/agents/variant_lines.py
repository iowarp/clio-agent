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

Both are ordinary plane ops (appends and deletes), recorded like any other.
"""

from __future__ import annotations

import logging
from typing import Any

from clio_agent.gact import context as _ctx

logger = logging.getLogger(__name__)


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


def fork_try(run_index: int) -> list[str]:
    """Start try ``run_index`` from the base scope; returns the forked segment ids."""
    arc, session, base = _plane()
    scope = _run_scope(base, run_index)
    _retire_live(arc, session, scope)
    return _copy(arc, session, scope, list(arc.render_working_set(session, base)))


def record_winner(run_index: int, forked: list[str]) -> None:
    """Continue the base scope with the winning try's line."""
    arc, session, base = _plane()
    live = list(arc.render_working_set(session, _run_scope(base, run_index)))
    if [seg.id for seg in live[: len(forked)]] == forked:
        new = live[len(forked) :]
    else:
        logger.info(
            "variant.winner.compacted agent=%s run_index=%d: base takes its working set",
            base,
            run_index,
        )
        _retire_live(arc, session, base)
        new = live
    _copy(arc, session, base, new)
