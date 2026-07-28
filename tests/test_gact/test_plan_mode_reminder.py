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
    _PLAN_FILE_METADATA_KEY,
    _PLAN_REMINDER_FULL_INTERVAL,
    _PLAN_REMINDER_STATE_KEY,
    PLAN_MODE_REMINDER_MARKER,
    inject_plan_mode_reminder,
    plan_file_exists,
    recorded_plan_file,
)
from clio_agent.gact.routes.compaction import build_compact_summary_message
from clio_agent.gact.runtime.grant_resolver import plans_dir, resolve

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


# --------------------------------------------------------------------------- #
# P1.3 #1065 — plan-file lifecycle (path + existence + create/edit guidance)   #
# --------------------------------------------------------------------------- #

_LEDGER_HEADERS = "Given / Learned / To look up / To derive"
_STRUCTURE_HINT = "Structure the plan to fit the task"
_STALENESS = "evaluate whether it is still relevant to THIS task"
_SHOW_THE_PLAN = "Show the plan to the user"
_CREATE_ONLY = "No plan file exists yet"
_EDIT_ONLY = "already exists at"


def test_plan_file_path_recorded_on_first_plan_turn(tmp_path: Path) -> None:
    """The deterministic plan-file path is computed and recorded on session.metadata."""
    app, sess = _plan_session(tmp_path)
    assert _PLAN_FILE_METADATA_KEY not in (sess.metadata or {})
    inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)

    fresh = app.state.sessions.get(sess.id)
    plan_file = fresh.metadata[_PLAN_FILE_METADATA_KEY]
    assert plan_file  # a non-empty path was recorded
    recorded = Path(plan_file)
    assert recorded.parent == plans_dir()  # lives directly under the plans dir
    assert recorded.suffix == ".md"
    assert recorded_plan_file(fresh) == plan_file


def test_plan_file_path_is_stable_across_turns(tmp_path: Path) -> None:
    """The recorded path does not change turn to turn (recorded once, re-read thereafter)."""
    app, sess = _plan_session(tmp_path)
    inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)
    first = app.state.sessions.get(sess.id).metadata[_PLAN_FILE_METADATA_KEY]
    for _ in range(3):
        inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)
    later = app.state.sessions.get(sess.id).metadata[_PLAN_FILE_METADATA_KEY]
    assert first == later


def test_recorded_plan_path_is_within_plan_acl_carveout(tmp_path: Path) -> None:
    """A write to the recorded plan path resolves ALLOW in plan mode (the @70 carve-out)."""
    app, sess = _plan_session(tmp_path)
    inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)
    plan_file = app.state.sessions.get(sess.id).metadata[_PLAN_FILE_METADATA_KEY]
    # No persisted user policies — ALLOW must come purely from the built-in plan-file carve-out.
    action = resolve(
        "tool",
        "fs_apply_edit_write",
        policies=[],
        session_id=sess.id,
        path=plan_file,
        mode="plan",
    )
    assert action == "allow"


def test_reminder_shows_create_guidance_when_file_absent(tmp_path: Path) -> None:
    """With no plan file on disk, the FULL reminder tells the model to CREATE it at the path."""
    app, sess = _plan_session(tmp_path)
    out = inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)
    plan_file = app.state.sessions.get(sess.id).metadata[_PLAN_FILE_METADATA_KEY]
    assert _CREATE_ONLY in out
    assert _EDIT_ONLY not in out
    assert plan_file in out
    assert not plan_file_exists(app.state.sessions.get(sess.id))


def test_reminder_shows_edit_guidance_when_file_present(tmp_path: Path) -> None:
    """Once the plan file exists on disk, the FULL reminder switches to incremental-edit guidance."""
    app, sess = _plan_session(tmp_path)
    # Turn 1 computes + records the path (file not yet written).
    inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)
    plan_file = Path(app.state.sessions.get(sess.id).metadata[_PLAN_FILE_METADATA_KEY])
    # The MODEL writes the plan (simulated) -> existence flips.
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text("# Plan\n", encoding="utf-8")
    assert plan_file_exists(app.state.sessions.get(sess.id))
    # Force a FULL reminder (post-compaction) so the create/edit branch is visible.
    _append_compaction(app, sess.id)
    out = inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)
    assert _FULL_ONLY in out
    assert _EDIT_ONLY in out
    assert _CREATE_ONLY not in out


def test_full_reminder_contains_ledger_structure_staleness_and_show_rules(tmp_path: Path) -> None:
    """The FULL reminder carries the epistemic ledger, structure hint, staleness + show rules."""
    app, sess = _plan_session(tmp_path)
    out = inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)
    assert _LEDGER_HEADERS in out
    assert _STRUCTURE_HINT in out
    assert _STALENESS in out
    assert _SHOW_THE_PLAN in out


def test_sparse_reminder_stays_a_one_liner(tmp_path: Path) -> None:
    """The SPARSE reminder is a single line: no ledger/structure/staleness/show guidance."""
    app, sess = _plan_session(tmp_path)
    inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)  # turn 1 -> full
    out = inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)  # turn 2 -> sparse
    assert out.count("\n\n---\n\n") == 1
    reminder = out.split("\n\n---\n\n", 1)[0]
    assert "\n" not in reminder  # the sparse block itself is one line
    for absent in (_LEDGER_HEADERS, _STRUCTURE_HINT, _STALENESS, _SHOW_THE_PLAN):
        assert absent not in out


@pytest.mark.parametrize("mode", ["edit", "architect"])
def test_non_plan_mode_records_no_plan_file(tmp_path: Path, mode: str) -> None:
    """edit/architect sessions get no plan reminder AND no recorded plan-file path."""
    app, sess = _plan_session(tmp_path, mode=mode)
    out = inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)
    assert out == _USER_TEXT
    fresh = app.state.sessions.get(sess.id)
    assert _PLAN_FILE_METADATA_KEY not in (fresh.metadata or {})
    assert recorded_plan_file(fresh) is None
