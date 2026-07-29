"""P1.6a #1068 — plan_workflow / plan_small variants shape planning.

The ``plan_workflow`` / ``plan_small`` enter_mode variants (skill_effects.py) record a
``plan_variant`` tag on ``session.metadata``. Before P1.6a that tag was consumed NOWHERE —
the variants changed nothing about planning. These tests pin the consumption contract:

  - ``plan_workflow`` shapes the FULL reminder into a workflow-grade scaffold (explicit
    numbered steps + per-step verification + a Risks & Dependencies section);
  - ``plan_small`` shapes it into the lightweight scaffold (short plan, minimal structure)
    with sparser full-reminder cadence;
  - NO tag reproduces the pre-P1.6 reminder BYTE-FOR-BYTE (the regression lock);
  - the variant is CLEARED once the session leaves plan mode (plan_exit approve/exit).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.app import build_app
from clio_agent.gact.plan_mode import (
    _PLAN_REMINDER_STATE_KEY,
    _plan_mode_reminder_block,
    inject_plan_mode_reminder,
    resolve_plan_exit_answer,
)
from clio_agent.gact.planning import (
    PLAN_VARIANT_METADATA_KEY,
    PLAN_VARIANT_SMALL,
    PLAN_VARIANT_WORKFLOW,
    plan_variant_guidance,
    recorded_plan_variant,
)

pytestmark = pytest.mark.usefixtures("host_agent_executor")

_USER_TEXT = "investigate the failing test"

# --------------------------------------------------------------------------- #
# (3) NO tag == pre-P1.6 output, BYTE-FOR-BYTE (golden captured from HEAD).    #
# --------------------------------------------------------------------------- #

_PLAN = "/tmp/plans/x.md"

_GOLDEN_FULL_CREATE = (
    "## Plan Mode active — read-only except the plan file\n\n"
    "You are in PLAN MODE. Investigate freely, but do NOT modify the system: every write, "
    "edit, and file-mutating tool is blocked.\n"
    "- No plan file exists yet. Create your plan at /tmp/plans/x.md (write a *.md there — it "
    "is the ONLY writable path in plan mode).\n"
    "- Structure the plan to fit the task: Simple change → Changes + Verification; Standard "
    "task → Objective, Key Files & Context, Implementation Steps, Verification; Complex / "
    "architectural → Background, Scope, Proposed Solution, Alternatives, a phased Plan, "
    "Verification, Migration/Rollback.\n"
    "- Keep an epistemic ledger of what you know vs. must find out, under the headers: "
    "Given / Learned / To look up / To derive.\n"
    "- If a plan already exists, evaluate whether it is still relevant to THIS task before "
    "editing; treat a new task as a fresh plan.\n"
    "- Show the plan to the user in your response — don't just write it to disk.\n"
    "- Turn-ending contract: when the plan is complete, END YOUR TURN and hand it back for "
    "approval — do NOT try to execute the plan while in plan mode."
)

_GOLDEN_FULL_EDIT = (
    "## Plan Mode active — read-only except the plan file\n\n"
    "You are in PLAN MODE. Investigate freely, but do NOT modify the system: every write, "
    "edit, and file-mutating tool is blocked.\n"
    "- A plan file already exists at /tmp/plans/x.md. Make incremental edits to it as you learn.\n"
    "- Structure the plan to fit the task: Simple change → Changes + Verification; Standard "
    "task → Objective, Key Files & Context, Implementation Steps, Verification; Complex / "
    "architectural → Background, Scope, Proposed Solution, Alternatives, a phased Plan, "
    "Verification, Migration/Rollback.\n"
    "- Keep an epistemic ledger of what you know vs. must find out, under the headers: "
    "Given / Learned / To look up / To derive.\n"
    "- If a plan already exists, evaluate whether it is still relevant to THIS task before "
    "editing; treat a new task as a fresh plan.\n"
    "- Show the plan to the user in your response — don't just write it to disk.\n"
    "- Turn-ending contract: when the plan is complete, END YOUR TURN and hand it back for "
    "approval — do NOT try to execute the plan while in plan mode."
)

_GOLDEN_SPARSE = (
    "## Plan Mode active — read-only except the plan file (/tmp/plans/x.md). Keep writing "
    "your plan there; end your turn to hand it back for approval rather than executing it "
    "yourself."
)


def test_no_variant_full_create_is_byte_identical() -> None:
    assert (
        _plan_mode_reminder_block(full=True, plan_file=_PLAN, exists=False) == _GOLDEN_FULL_CREATE
    )


def test_no_variant_full_edit_is_byte_identical() -> None:
    assert _plan_mode_reminder_block(full=True, plan_file=_PLAN, exists=True) == _GOLDEN_FULL_EDIT


def test_no_variant_sparse_is_byte_identical() -> None:
    assert _plan_mode_reminder_block(full=False, plan_file=_PLAN, exists=False) == _GOLDEN_SPARSE


# --------------------------------------------------------------------------- #
# (1) plan_workflow => workflow-grade scaffold in the reminder/prompt path.    #
# --------------------------------------------------------------------------- #


def _plan_session(tmp_path: Path, *, variant: str = "", mode: str = "plan"):
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode=mode)
    if variant:
        app.state.sessions.update(sess.id, metadata_patch={PLAN_VARIANT_METADATA_KEY: variant})
    return app, app.state.sessions.get(sess.id)


def test_plan_workflow_full_reminder_is_workflow_scaffold(tmp_path: Path) -> None:
    app, sess = _plan_session(tmp_path, variant=PLAN_VARIANT_WORKFLOW)
    out = inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)
    # Numbered, per-step-verified workflow structure + an explicit risks/dependencies section.
    assert "numbered" in out.lower()
    assert "Risks & Dependencies" in out
    # The generic default structure sentence is REPLACED by the workflow one.
    assert "Structure the plan to fit the task: Simple change" not in out
    assert out.endswith(_USER_TEXT)


def test_plan_small_full_reminder_is_lightweight(tmp_path: Path) -> None:
    app, sess = _plan_session(tmp_path, variant=PLAN_VARIANT_SMALL)
    out = inject_plan_mode_reminder(app, sess.id, sess, _USER_TEXT)
    assert "short and lightweight" in out
    # No workflow scaffold and no heavy default structure sentence.
    assert "Risks & Dependencies" not in out
    assert "Structure the plan to fit the task: Simple change" not in out


def test_plan_small_full_reminder_cadence_is_sparser(tmp_path: Path) -> None:
    """plan_small full reminders are SPARSER than the default 10-turn cadence."""
    default = plan_variant_guidance("")
    small = plan_variant_guidance(PLAN_VARIANT_SMALL)
    assert small.full_interval > default.full_interval


# A window position strictly between the default (10) and small (20) full-reminder intervals:
# the default cadence has elapsed here, the small one has NOT.
_BETWEEN_INTERVALS_DELTA = 15

# Sentinels distinguishing the FULL contract from the SPARSE one-liner at the inject boundary.
_FULL_ONLY = "Turn-ending contract"
_SPARSE_ONLY = "Keep writing your plan there"


def _seed_reminder_window(app: Any, sid: str, *, delta: int) -> None:
    """Seed the reminder suppression counter so the NEXT inject sits ``delta`` turns past the last
    full re-inject, with no compaction since — isolating the cadence window from first-turn and
    post-compaction triggers so only ``guidance.full_interval`` decides full-vs-sparse."""

    app.state.sessions.update(
        sid,
        metadata_patch={
            _PLAN_REMINDER_STATE_KEY: {
                # inject bumps turn_index to `1 + delta`; last_full=1 → window delta = `delta`.
                "turn_index": delta,
                "last_full_turn": 1,
                "compactions_at_last_full": 0,
            }
        },
    )


def test_plan_small_cadence_is_sparser_through_inject(tmp_path: Path) -> None:
    """Drive ``inject_plan_mode_reminder`` at a window position between the two intervals: a
    no-variant session (interval 10) re-injects the FULL block while a plan_small session
    (interval 20) still gets the SPARSE one-liner. This pins the sparser cadence at the CALL
    SITE — replacing ``guidance.full_interval`` in the composer with any fixed interval flips it
    red — where the dataclass-field comparison in
    ``test_plan_small_full_reminder_cadence_is_sparser`` cannot.
    """

    (tmp_path / "d").mkdir()
    (tmp_path / "s").mkdir()

    app_d, sess_d = _plan_session(tmp_path / "d")
    _seed_reminder_window(app_d, sess_d.id, delta=_BETWEEN_INTERVALS_DELTA)
    out_default = inject_plan_mode_reminder(
        app_d, sess_d.id, app_d.state.sessions.get(sess_d.id), _USER_TEXT
    )
    assert _FULL_ONLY in out_default  # default 10-turn cadence has elapsed → FULL

    app_s, sess_s = _plan_session(tmp_path / "s", variant=PLAN_VARIANT_SMALL)
    _seed_reminder_window(app_s, sess_s.id, delta=_BETWEEN_INTERVALS_DELTA)
    out_small = inject_plan_mode_reminder(
        app_s, sess_s.id, app_s.state.sessions.get(sess_s.id), _USER_TEXT
    )
    assert _SPARSE_ONLY in out_small  # small 20-turn cadence has NOT elapsed → SPARSE
    assert _FULL_ONLY not in out_small


def test_recorded_plan_variant_reads_metadata(tmp_path: Path) -> None:
    _, sess = _plan_session(tmp_path, variant=PLAN_VARIANT_WORKFLOW)
    assert recorded_plan_variant(sess) == PLAN_VARIANT_WORKFLOW
    _, plain = _plan_session(tmp_path)
    assert recorded_plan_variant(plain) == ""


# --------------------------------------------------------------------------- #
# (4) the variant CLEARS once the session leaves plan mode (plan_exit resume). #
# --------------------------------------------------------------------------- #


def _fake_deps() -> SimpleNamespace:
    calls: dict[str, list[Any]] = {"resume": [], "replace": []}

    def start_background_user_turn(sid, sess, user_text, *, metadata=None, prev_status="", **kw):
        calls["resume"].append({"sid": sid, "text": user_text, "metadata": metadata or {}})
        return SimpleNamespace(id=f"msg_resume_{len(calls['resume'])}")

    def replace_session_messages(app, sid, messages):
        calls["replace"].append({"sid": sid, "messages": messages})

    return SimpleNamespace(
        start_background_user_turn=start_background_user_turn,
        replace_session_messages=replace_session_messages,
        _calls=calls,
    )


def _pending_plan_exit_question(app: Any, sess: Any, *, plan_file: str):
    from clio_agent.gact.plan_mode import PLAN_EXIT_APPROVAL_META, _plan_exit_options
    from clio_agent.gact.types import UserQuestion

    q = UserQuestion(
        id="q_plan_exit",
        session_id=sess.id,
        prompt="approve?",
        status="pending",
        kind="choice",
        options=_plan_exit_options(),
        created_at="2026-07-27T00:00:00+00:00",
        updated_at="2026-07-27T00:00:00+00:00",
        source="plan_exit",
        metadata={PLAN_EXIT_APPROVAL_META: True, "resume_on_answer": True, "plan_file": plan_file},
    )
    app.state.user_questions[q.id] = q
    app.state.sessions.update(
        sess.id, status="waiting_user", metadata_patch={"pending_user_question_id": q.id}
    )
    app.state.agent = object()
    return q


def _answer(question: Any, *, selected: list[str], answer: str = ""):
    return question.model_copy(
        update={"status": "answered", "selected_options": selected, "answer": answer}
    )


def test_plan_variant_cleared_after_plan_exit_approve(tmp_path: Path) -> None:
    app, sess = _plan_session(tmp_path, variant=PLAN_VARIANT_WORKFLOW)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n", encoding="utf-8")
    app.state.sessions.update(sess.id, metadata_patch={"plan_file": str(plan_file)})
    assert recorded_plan_variant(app.state.sessions.get(sess.id)) == PLAN_VARIANT_WORKFLOW

    q = _pending_plan_exit_question(app, app.state.sessions.get(sess.id), plan_file=str(plan_file))
    resolve_plan_exit_answer(app, _fake_deps(), sess.id, _answer(q, selected=["auto"]))

    fresh = app.state.sessions.get(sess.id)
    assert fresh.mode == "edit"  # left plan mode
    assert recorded_plan_variant(fresh) == ""  # variant cleared


def test_plan_variant_preserved_after_plan_exit_reject(tmp_path: Path) -> None:
    """A rejected plan-exit stays in plan mode — the variant must survive so the revision turn
    still gets the same scaffold."""
    app, sess = _plan_session(tmp_path, variant=PLAN_VARIANT_WORKFLOW)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n", encoding="utf-8")
    app.state.sessions.update(sess.id, metadata_patch={"plan_file": str(plan_file)})

    q = _pending_plan_exit_question(app, app.state.sessions.get(sess.id), plan_file=str(plan_file))
    resolve_plan_exit_answer(
        app, _fake_deps(), sess.id, _answer(q, selected=["reject"], answer="add rollback")
    )

    fresh = app.state.sessions.get(sess.id)
    assert fresh.mode == "plan"  # stayed in plan mode
    assert recorded_plan_variant(fresh) == PLAN_VARIANT_WORKFLOW  # variant preserved
