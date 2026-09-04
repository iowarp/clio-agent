"""Which forward kwargs carry MODEL INPUT, and what to record when one is dropped.

``images``/``files`` are not runtime mode flags: they are the resource's own
bytes on their way to the model. Threading them into a ``forward`` that never
declared them raised ``TypeError`` on every rung of the streaming compat ladder
(all rungs carried the same kwargs), failing ordinary IMAGELESS turns on every
pre-multimodal module. Dropping them silently is the opposite defect — the
answer looks fine and the attachment simply never arrived.

This module owns both halves: the predicate that decides whether a given agent
can receive native inputs, and the typed record for a drop that actually lost
something. The typed reason comes from the audited stream-fallback catalog but
is recorded as a NOTE, not in the single delivery-path slot: the text still
streamed, so the slot's question ("how was this turn delivered?") is untouched,
and a later delivery reason must not be able to overwrite this one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from clio_agent.gact.messaging import _agent_accepts_images
from clio_agent.gact.stream_fallbacks import record_stream_fallback_note

if TYPE_CHECKING:
    from fastapi import FastAPI

#: The forward kwargs that carry MODEL INPUT rather than a runtime mode flag.
MODEL_INPUT_KWARGS: frozenset[str] = frozenset({"images", "files"})

#: The audited catalog reason for a native input that never reached the model.
NATIVE_INPUTS_DROPPED_REASON = "native_model_inputs_dropped"


def record_dropped_model_inputs(
    app: "FastAPI",
    sid: str,
    dropped: list[str],
    candidate: dict[str, Any],
    *,
    callee: str,
) -> None:
    """Record a typed reason when a call drops model inputs that carried content.

    A dropped kwarg that carried NOTHING (the empty-list default every text turn
    passes) is not a degradation and stays unrecorded; a dropped kwarg carrying
    real images/files is the silent-drop defect and gets the typed note.

    Args:
        app: The GACT app owning the note ledger.
        sid: The session the degradation belongs to.
        dropped: Kwarg names the callee did not accept.
        candidate: The full kwarg mapping that was offered.
        callee: Human-readable name of the callee, for the message.
    """

    carried = [name for name in dropped if name in MODEL_INPUT_KWARGS and bool(candidate.get(name))]
    if not carried:
        return
    counts = ", ".join(f"{name}={len(candidate.get(name) or [])}" for name in carried)
    record_stream_fallback_note(
        app,
        sid,
        NATIVE_INPUTS_DROPPED_REASON,
        f"{callee} accepts no {', '.join(carried)} parameter; {counts} "
        "native input(s) were not delivered to the model.",
    )


def native_input_kwargs(
    app: "FastAPI",
    sid: str,
    agent: Any,
    *,
    images: list[Any] | None,
    files: list[Any] | None,
) -> dict[str, Any]:
    """Return the native-input kwargs to thread into ``agent``'s forward.

    Empty when the agent's forward declares no such parameter — in which case a
    non-empty attachment is recorded as a typed note before returning, so the
    drop is never silent. The predicate is the SAME one the turn path uses to
    decide native dispatch, so the gate and the dispatch cannot disagree.
    """

    native = {"images": list(images or []), "files": list(files or [])}
    if _agent_accepts_images(agent):
        return native
    record_dropped_model_inputs(
        app, sid, sorted(native), native, callee=f"{type(agent).__name__}.forward"
    )
    return {}


__all__ = [
    "MODEL_INPUT_KWARGS",
    "NATIVE_INPUTS_DROPPED_REASON",
    "native_input_kwargs",
    "record_dropped_model_inputs",
]
