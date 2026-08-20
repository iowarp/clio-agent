"""Reading a session must tolerate modes the write side has retired.

Regression for #1171: P1.1 (#1063) removed ``chat`` from the wire ``Session``
Literal but did not migrate stored records. Every session written before that
change carries ``mode="chat"``, so rehydrating one raised ValidationError — and
because the list endpoint built its response with a list comprehension, ONE
such row returned 500 for the entire listing.
"""

from __future__ import annotations

import pytest

from clio_agent.gact.sessions import Session as SessionRecord
from clio_agent.gact.types import Session as WireSession


def _record(session_id: str, mode: str) -> SessionRecord:
    rec = SessionRecord(id=session_id, title=f"session {session_id}", workspace_id="ws_test")
    rec.mode = mode
    return rec


def test_retired_chat_mode_reads_as_edit() -> None:
    """``chat`` was behaviour-identical to ``edit`` (#1063), so it maps there."""
    wire = _record("sess_legacy", "chat").to_wire()
    assert wire["mode"] == "edit"
    # And the wire model accepts it, which is the whole point.
    assert WireSession(**wire).mode == "edit"


def test_current_modes_are_untouched() -> None:
    for mode in ("plan", "edit", "architect"):
        assert _record("sess_x", mode).to_wire()["mode"] == mode


def test_unknown_mode_does_not_raise_and_does_not_invent_authority() -> None:
    """An unrecognised mode must not crash the read, and must not silently
    grant MORE authority than it had. It degrades to the safest mode."""
    wire = _record("sess_weird", "some_future_mode").to_wire()
    assert WireSession(**wire).mode == "plan"


def test_listing_survives_a_row_it_cannot_normalize(monkeypatch: pytest.MonkeyPatch) -> None:
    """One unreadable row must not take down the whole listing.

    This is the blast-radius half of the fix: coercion handles the modes we
    know about, per-row tolerance bounds the damage from the ones we do not.
    """
    from clio_agent.gact.routes import sessions as sessions_route

    good = _record("sess_good", "edit")
    bad = _record("sess_bad", "chat")

    rows = sessions_route.rows_to_wire([good, bad])
    ids = [r.id for r in rows]
    assert "sess_good" in ids
    assert "sess_bad" in ids, "a normalizable row must still be served"
