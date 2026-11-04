"""Invocation-context bridges from the tool observer into artifact mint seams.

Owner module (no-accretion ground rule): ``minting.py`` and ``tool_observer.py``
are both already at their per-file ratchet ceiling (``scripts/check_file_size.py``),
so a NEW observer-thread-local accessor a mint seam needs does not get appended to
either — it lives here instead. ``minting._observer_call_started_at`` (the sibling
accessor for the call-start epoch) stays where it is, unchanged.
"""

from __future__ import annotations

import threading
from contextvars import ContextVar
from typing import Any

#: Set by the agent loop around one model tool call: the observer fills in the call id
#: it mints, so the loop can show the model a citable reference to that exact call
#: (the science log's ``call:<session>/<call_id>``). The first call reported wins, so a
#: tool that runs other tools keeps its own id.
MODEL_CALL_SLOT: ContextVar[dict[str, str] | None] = ContextVar("model_call_slot", default=None)


def observer_call_id() -> str:
    """Read the real invoking call ID across coroutine/worker handoffs, or ``""``."""
    try:
        from clio_agent.gact.tool_observer import _OBSERVER_CALL_IDS  # noqa: PLC0415

        return str(_OBSERVER_CALL_IDS.get() or "")
    except Exception:  # noqa: BLE001 — the skip is an optimization, never load-bearing
        return ""


def current_call_id() -> str:
    """The observer's id of the model tool call now running, or ``""``.

    The agent loop's slot (filled when the observer starts the call) first, since it
    survives the hand-off into a native tool's worker; else the observer's own var.
    """
    slot = MODEL_CALL_SLOT.get()
    return str((slot or {}).get("call_id") or "") or observer_call_id()


def report_observer_call(call_id: str) -> None:
    """Tell the agent loop which observer call the current model tool call is."""
    slot = MODEL_CALL_SLOT.get()
    if slot is not None and "call_id" not in slot:
        slot["call_id"] = call_id


#: Versions a tool minted itself during one call, outside the workspace's declared-output
#: channel (a browser screenshot in the conversation's scratch folder). The observer takes
#: them when the call completes, so they become that call's generated outputs and share
#: its transform with the call's used edges. Keyed by observer call id.
_CALL_OUTPUTS: dict[str, list[Any]] = {}
_CALL_OUTPUTS_LOCK = threading.Lock()


def note_call_output(version: Any) -> str:
    """Record ``version`` as an output of the observed call now running; returns its id."""
    call_id = observer_call_id()
    if call_id and version is not None:
        with _CALL_OUTPUTS_LOCK:
            _CALL_OUTPUTS.setdefault(call_id, []).append(version)
    return call_id


def take_call_outputs(call_id: str) -> list[Any]:
    """The versions noted for ``call_id``, once."""
    with _CALL_OUTPUTS_LOCK:
        return _CALL_OUTPUTS.pop(call_id, []) if call_id else []


__all__ = [
    "MODEL_CALL_SLOT",
    "current_call_id",
    "note_call_output",
    "observer_call_id",
    "report_observer_call",
    "take_call_outputs",
]
