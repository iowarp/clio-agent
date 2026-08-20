"""Thread-local bridges from the tool observer into the artifact mint seams.

Owner module (no-accretion ground rule): ``minting.py`` and ``tool_observer.py``
are both already at their per-file ratchet ceiling (``scripts/check_file_size.py``),
so a NEW observer-thread-local accessor a mint seam needs does not get appended to
either — it lives here instead. ``minting._observer_call_started_at`` (the sibling
accessor for the call-start epoch) stays where it is, unchanged.
"""

from __future__ import annotations


def observer_call_id() -> str:
    """The current tool call's ``call_id`` from the observer thread-local, or ``""``.

    A native tool (e.g. ``create_artifact``) runs SYNCHRONOUSLY between the tool
    observer's ``started`` (which stamps ``tool_observer._OBSERVER_CALL_IDS``
    before the tool body executes) and ``completed`` phases, on the SAME thread —
    so a mint seam invoked from inside the tool body can read the invoking call's
    real id here (A8, #1176) instead of leaving ``producer.call_id`` empty. ``""``
    outside an observed call (e.g. a direct unit-test invocation that bypasses the
    tool-call wrapper) — never invented.
    """
    try:
        from clio_agent.gact.tool_observer import _OBSERVER_CALL_IDS  # noqa: PLC0415

        return str(getattr(_OBSERVER_CALL_IDS, "value", None) or "")
    except Exception:  # noqa: BLE001 — the skip is an optimization, never load-bearing
        return ""


__all__ = ["observer_call_id"]
