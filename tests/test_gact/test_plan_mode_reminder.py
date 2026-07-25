"""P1.2 #1064 — periodic plan-mode reminder attachment.

Covers ``enrichment.inject_plan_mode_reminder``, the per-turn attachment that surfaces
plan mode to the model and survives compaction (a system-prompt-only reminder is lost
once the prefix is compacted). Asserts:

  - a FULL reminder on the first plan-mode turn;
  - a SPARSE one-liner within the suppression window;
  - a FULL re-inject on the turn immediately after a compaction;
  - a FULL re-inject once the suppression window elapses;
  - edit/architect turns get NO attachment (text returned unchanged);
  - the suppression counter lives on ``session.metadata`` — no new store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.gact.app import build_app
from clio_agent.gact.plan_mode import (
    _PLAN_REMINDER_FULL_INTERVAL,
    _PLAN_REMINDER_STATE_KEY,
    PLAN_MODE_REMINDER_MARKER,
    inject_plan_mode_reminder,
)
from clio_agent.gact.routes.compaction import build_compact_summary_message
from clio_agent.gact.runtime.grant_resolver import plans_dir

pytestmark = pytest.mark.usefixtures("host_agent_executor")

# Sentinels distinguishing the full contract from the sparse one-liner.
_FULL_ONLY = "Turn-ending contract"
_SPARSE_ONLY = "Keep writing your plan there"
_USER_TEXT = "investigate the failing test"


def _plan_session(tmp_path: Path, mode: str = "plan"):
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode=mode)
    return app, sess


def _append_compaction(app, sid: str) -> None:
    app.state.messages.setdefault(sid, []).append(
        build_compact_summary_message(
            session_id=sid,
            turn_id="turn_x",
            summary="prior work summarized",
            event_id="evt_x",
            compacted_message_ids=["m1", "m2"],
        )
    )


def test_first_plan_turn_gets_full_reminder(tmp_path: Path) -> None:
    app, sess = _plan_session(tmp_path)
    out = inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)
    assert PLAN_MODE_REMINDER_MARKER in out
    assert _FULL_ONLY in out
    assert str(plans_dir()) in out
    # The original user text is preserved after the attachment.
    assert out.endswith(_USER_TEXT)


def test_second_plan_turn_is_sparse_within_window(tmp_path: Path) -> None:
    app, sess = _plan_session(tmp_path)
    inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)  # turn 1 -> full
    out = inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)  # turn 2 -> sparse
    assert PLAN_MODE_REMINDER_MARKER in out
    assert _SPARSE_ONLY in out
    assert _FULL_ONLY not in out
    assert str(plans_dir()) in out


def test_post_compaction_turn_reinjects_full(tmp_path: Path) -> None:
    app, sess = _plan_session(tmp_path)
    inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)  # turn 1 -> full
    out_sparse = inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)  # turn 2 -> sparse
    assert _FULL_ONLY not in out_sparse
    # A compaction drops the earlier reminder from the model's view -> re-inject full.
    _append_compaction(app, sess.id)
    out_full = inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)  # turn 3 -> full again
    assert _FULL_ONLY in out_full


def test_full_reinjects_once_window_elapses(tmp_path: Path) -> None:
    app, sess = _plan_session(tmp_path)
    seen_full = []
    for _ in range(_PLAN_REMINDER_FULL_INTERVAL + 1):
        out = inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)
        seen_full.append(_FULL_ONLY in out)
    # Turn 1 full, then sparse until the window elapses at turn (interval+1).
    assert seen_full[0] is True
    assert seen_full[1] is False
    assert seen_full[_PLAN_REMINDER_FULL_INTERVAL] is True


@pytest.mark.parametrize("mode", ["edit", "architect"])
def test_non_plan_mode_gets_no_attachment(tmp_path: Path, mode: str) -> None:
    app, sess = _plan_session(tmp_path, mode=mode)
    out = inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)
    assert out == _USER_TEXT
    assert PLAN_MODE_REMINDER_MARKER not in out
    # No suppression state is created for a non-plan session.
    assert _PLAN_REMINDER_STATE_KEY not in (sess.metadata or {})


def test_suppression_state_lives_on_session_metadata(tmp_path: Path) -> None:
    app, sess = _plan_session(tmp_path)
    inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)
    fresh = app.state.sessions.get(sess.id)
    state = fresh.metadata[_PLAN_REMINDER_STATE_KEY]
    assert state["turn_index"] == 1
    assert state["last_full_turn"] == 1
    assert state["compactions_at_last_full"] == 0
