"""What one compaction changes around the context it folds: the session slot and usage.

* A manual compaction (``POST /v1/sessions/{sid}/compact``, a plugin change applied by
  compacting) holds the session's conversation slot for its whole run, like any other
  finite runtime change (:meth:`~clio_agent.gact.turn_runner.TurnRunner.session_change`):
  while a turn runs it is refused ``409 session_busy`` (that turn's loop compacts its own
  context at its step boundaries), and no turn starts while it folds.
* After a fold the session's stored per-scope usage (``context_usage_by_scope``, written
  when a turn finalizes) still measures the context from before it. The next turn's
  first step boundary reads that stored value when it has no measurement of its own
  (:func:`~clio_agent.gact.compaction._durable_prompt_tokens`), so a stale one compacted
  again at once with an LM call. The folded scope's row is marked ``estimated`` with the
  live context's estimate, which that check does not count as a measurement.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from typing import Any

__all__ = ["compaction_slot", "usage_after_fold"]


@contextmanager
def compaction_slot(app: Any, sid: str, trigger: str) -> Iterator[None]:
    """Hold ``sid``'s conversation slot for a manual compaction (the auto one runs inside
    the turn that owns it).

    Raises:
        CompactionError: 409 ``session_busy`` while a turn (or another change) holds it.
    """
    from clio_agent.gact.compaction import CompactionError  # noqa: PLC0415
    from clio_agent.gact.turn_runner import BUSY_ERROR_CODE, TurnAdmissionError  # noqa: PLC0415

    runner = getattr(app.state, "turn_runner", None)
    with ExitStack() as stack:
        # An app with no turn runner (a bare test app) runs no turns to conflict with.
        if trigger == "manual" and runner is not None:
            try:
                stack.enter_context(runner.session_change(sid))
            except TurnAdmissionError as exc:
                raise CompactionError(
                    409,
                    BUSY_ERROR_CODE,
                    "a turn is running in this session; it compacts its own context, "
                    "so compact again once it settles",
                    {"session_id": sid},
                ) from exc
        yield


def usage_after_fold(app: Any, sid: str, scopes: Iterable[str]) -> None:
    """Mark the stored usage of each folded scope ``estimated`` (the live estimate)."""
    session = app.state.sessions.get(sid)
    metadata = getattr(session, "metadata", None)
    stored = metadata.get("context_usage_by_scope") if isinstance(metadata, Mapping) else None
    if not isinstance(stored, Mapping):
        return  # no turn has recorded usage yet: nothing is stale
    usage = dict(stored)
    now = datetime.now(timezone.utc).isoformat()
    for scope in scopes:
        key = scope.partition("#run")[0]
        row = usage.get(key)
        if isinstance(row, Mapping) and row.get("source") != "estimated":
            live = sum(app.state.arc.segment_tokens_by_kind(sid, scope).values())
            usage[key] = {
                **row,
                "source": "estimated",
                "used_tokens": int(live),
                "recorded_at": now,
                "estimated_after": "compaction",
            }
    if usage != stored:
        app.state.sessions.update(sid, metadata_patch={"context_usage_by_scope": usage})
