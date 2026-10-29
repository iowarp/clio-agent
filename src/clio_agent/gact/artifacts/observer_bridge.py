"""Invocation-context bridges from the tool observer into artifact mint seams.

Owner module (no-accretion ground rule): ``minting.py`` and ``tool_observer.py``
are both already at their per-file ratchet ceiling (``scripts/check_file_size.py``),
so a NEW observer-thread-local accessor a mint seam needs does not get appended to
either — it lives here instead. ``minting._observer_call_started_at`` (the sibling
accessor for the call-start epoch) stays where it is, unchanged.
"""

from __future__ import annotations

from contextvars import ContextVar

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


__all__ = ["MODEL_CALL_SLOT", "current_call_id", "observer_call_id", "report_observer_call"]
