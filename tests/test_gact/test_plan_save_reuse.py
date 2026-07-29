"""P1.6c #1068 — save-and-reuse of approved plans.

Covers the three-part contract:

* (1) REGISTER — a plan-exit APPROVE registers the approved plan file as a provenance-tracked
  artifact through the ONE ``promote_proposal`` path (registry row + version chain + producer
  provenance), recording the artifact ref on ``session.metadata`` (no fifth store);
* (2) DEGRADE — a registration failure is TYPED and NON-FATAL: the plan-exit resume still
  proceeds, the degrade reason is recorded on the session record AND emitted on the semantic
  highway (sabotage-proof — silence the emission and the sabotage test goes red);
* (3) GENERALIZE + REUSE — a pure ``planning.playbook_from_saved_plan`` derives a Playbook
  skeleton from a saved plan (step names kept, session-specific paths/values genericized), a
  skill can declare its playbook BY REFERENCE to a saved plan artifact (typed reject on a
  dangling ref), and a session with no saved plan behaves byte-identically to P1.6a/b.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents.skill_runtime import SkillRuntime, build_load_skill_tool
from clio_agent.gact.app import build_app
from clio_agent.gact.plan_mode import (
    _plan_exit_options,
    inject_plan_mode_reminder,
    plan_mode_reminder_block,
    resolve_plan_exit_answer,
)
from clio_agent.gact.plan_reuse import (
    SAVED_PLAN_KIND,
    SAVED_PLAN_METADATA_KEY,
    PlanReuseError,
    record_plan_playbook,
    resolve_saved_plan_playbook,
    save_approved_plan,
)
from clio_agent.gact.planning import (
    PlaybookError,
    playbook_from_saved_plan,
    recorded_playbook,
)
from clio_agent.gact.skills import SkillCatalog
from clio_agent.gact.types import AgentDef, UserQuestion

_PLAN_MD = """# Plan

## Objective
Do a thing.

## Implementation Steps
1. Read the config at `config/settings.toml` and note the model id.
2. Patch src/clio_agent/foo.py to add the new branch.
3. Run the tests and verify they pass.

## Verification
Run pytest.
"""


# --------------------------------------------------------------------------- #
# harness helpers                                                             #
# --------------------------------------------------------------------------- #


def _plan_app(tmp_path: Path):
    """Build an app whose ws_default workspace root is the tmp dir (contained plan-file case)."""
    app = build_app(sessions_path=tmp_path / "s.json")
    app.state.workspaces.update("ws_default", root_path=str(tmp_path))
    return app


def _plan_session(app: Any, tmp_path: Path, *, plan_text: str = _PLAN_MD, mode: str = "plan"):
    sess = app.state.sessions.create(
        workspace_id="ws_default", title="fix the failing test", mode=mode
    )
    plan_file = tmp_path / "fix-the-failing-test.md"
    plan_file.write_text(plan_text, encoding="utf-8")
    app.state.sessions.update(sess.id, metadata_patch={"plan_file": str(plan_file)})
    return app.state.sessions.get(sess.id), plan_file


def _capture_semantic_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def _fake_emit(app, sid, event_type, **kwargs):  # noqa: ANN001
        events.append({"event_type": event_type, "sid": sid, **kwargs})
        return None

    monkeypatch.setattr("clio_agent.gact.runtime.globals._emit_semantic_event", _fake_emit)
    return events


def _pending_question(app: Any, sess: Any, plan_file: str) -> UserQuestion:
    q = UserQuestion(
        id="q_plan_exit",
        session_id=sess.id,
        prompt="approve?",
        status="pending",
        kind="choice",
        options=_plan_exit_options(),
        created_at="2026-07-29T00:00:00+00:00",
        updated_at="2026-07-29T00:00:00+00:00",
        source="plan_exit",
        metadata={
            "plan_exit_approval": True,
            "resume_on_answer": True,
            "plan_file": plan_file,
            "recommended_mode": "auto",
        },
    )
    app.state.user_questions[q.id] = q
    app.state.sessions.update(
        sess.id, status="waiting_user", metadata_patch={"pending_user_question_id": q.id}
    )
    app.state.agent = object()
    return q


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


def _answer(question: UserQuestion, *, selected: list[str], answer: str = "") -> UserQuestion:
    return question.model_copy(
        update={"status": "answered", "selected_options": selected, "answer": answer}
    )


# --------------------------------------------------------------------------- #
# (1) REGISTER                                                                #
# --------------------------------------------------------------------------- #


def test_save_approved_plan_registers_artifact_with_provenance(tmp_path: Path) -> None:
    """save_approved_plan mints a provenance-tracked artifact + records the ref on session.metadata."""
    app = _plan_app(tmp_path)
    sess, plan_file = _plan_session(app, tmp_path)

    ref = save_approved_plan(app, sess.id, plan_file=str(plan_file))

    assert ref["saved"] is True
    assert ref["kind"] == SAVED_PLAN_KIND  # 'plan' is RESERVED — a plan doc rides an existing kind
    # The registry carries a real version chain for the saved plan.
    from clio_agent.gact.artifacts.registry import get_registry

    record = get_registry(app).get("ws_default", ref["name"])
    assert record is not None and record.head is not None
    head = record.head
    assert head.artifact_id == ref["artifact_id"]
    assert head.sha256 == ref["sha256"]
    # Provenance linkage: the producing session is recorded on the version's producer edge.
    assert head.producer.get("session_id") == sess.id
    assert head.producer.get("designation")  # a designation basis was stamped
    # The ref rides session.metadata (no fifth store).
    fresh = app.state.sessions.get(sess.id)
    assert fresh.metadata[SAVED_PLAN_METADATA_KEY]["artifact_id"] == ref["artifact_id"]


def test_plan_exit_approve_triggers_registration(tmp_path: Path) -> None:
    """A plan-exit APPROVE (via resolve_plan_exit_answer) registers the plan + resumes execution."""
    app = _plan_app(tmp_path)
    sess, plan_file = _plan_session(app, tmp_path)
    q = _pending_question(app, sess, str(plan_file))
    deps = _fake_deps()

    resolve_plan_exit_answer(app, deps, sess.id, _answer(q, selected=["auto"]))

    fresh = app.state.sessions.get(sess.id)
    assert fresh.mode == "edit"  # transition still fired
    assert len(deps._calls["resume"]) == 1  # execution resumed
    saved = fresh.metadata.get(SAVED_PLAN_METADATA_KEY)
    assert saved is not None and saved.get("saved") is True and saved.get("artifact_id")


# --------------------------------------------------------------------------- #
# (2) DEGRADE — typed + non-fatal + sabotage-proof                           #
# --------------------------------------------------------------------------- #


def test_registration_failure_is_typed_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registration EXCEPTION degrades to a typed reason on the session record — never raised."""
    app = _plan_app(tmp_path)
    sess, plan_file = _plan_session(app, tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr("clio_agent.gact.plan_reuse.promote_proposal", _boom)

    ref = save_approved_plan(app, sess.id, plan_file=str(plan_file))  # must NOT raise

    assert ref["saved"] is False
    assert ref["reason"] == "save_failed_exception"
    fresh = app.state.sessions.get(sess.id)
    assert fresh.metadata[SAVED_PLAN_METADATA_KEY]["reason"] == "save_failed_exception"


def test_registration_failure_emits_typed_highway_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sabotage-proof: the degrade reason reaches the semantic highway. Silence the plan.saved
    emission in plan_reuse and THIS assertion goes red (no-silent-fallback)."""
    app = _plan_app(tmp_path)
    sess, plan_file = _plan_session(app, tmp_path)
    events = _capture_semantic_events(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr("clio_agent.gact.plan_reuse.promote_proposal", _boom)

    save_approved_plan(app, sess.id, plan_file=str(plan_file))

    saved_events = [e for e in events if e["event_type"] == "plan.saved"]
    assert saved_events, "a plan.saved degrade event must reach the highway"
    degrade = saved_events[-1]
    assert degrade.get("status") == "failed"
    assert degrade["payload"]["reason"] == "save_failed_exception"


def test_plan_exit_resume_proceeds_despite_save_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degraded save NEVER blocks the plan-exit resume — the plan still executes."""
    app = _plan_app(tmp_path)
    sess, plan_file = _plan_session(app, tmp_path)
    q = _pending_question(app, sess, str(plan_file))
    deps = _fake_deps()

    monkeypatch.setattr(
        "clio_agent.gact.plan_reuse.promote_proposal",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    resolve_plan_exit_answer(app, deps, sess.id, _answer(q, selected=["auto"]))

    fresh = app.state.sessions.get(sess.id)
    assert fresh.mode == "edit"
    assert len(deps._calls["resume"]) == 1  # resumed despite the degraded save
    assert fresh.metadata[SAVED_PLAN_METADATA_KEY]["saved"] is False


# --------------------------------------------------------------------------- #
# (3a) GENERALIZE — derive a Playbook skeleton from a saved plan             #
# --------------------------------------------------------------------------- #


def test_playbook_from_saved_plan_derives_generalized_steps() -> None:
    """The pure derive lifts numbered steps into a Playbook, genericizing paths/values."""
    pb = playbook_from_saved_plan(_PLAN_MD, name="fix-plan")
    assert pb is not None
    assert pb.name == "fix-plan"
    names = [s.name for s in pb.steps]
    assert len(names) == 3  # the three numbered implementation steps
    # Session-specific literals are stripped to placeholders (no concrete paths/values remain).
    joined = " ".join(names)
    assert "config/settings.toml" not in joined
    assert "src/clio_agent/foo.py" not in joined
    assert "<path>" in joined and "<value>" in joined
    # Step names + verification intent are kept.
    assert any("verify" in n.lower() for n in names)
    # A generalized skeleton drops session-specific per-step narrowing.
    assert all(s.tools_allowed == () for s in pb.steps)


def test_playbook_from_unstructured_plan_is_typed_error() -> None:
    with pytest.raises(PlaybookError) as exc:
        playbook_from_saved_plan("just some prose with no steps or headings", name="x")
    assert exc.value.reason == "unstructured_plan"


def test_derived_playbook_drives_reminder_skeleton() -> None:
    """A derived playbook renders as the required plan skeleton in the full reminder block."""
    pb = playbook_from_saved_plan(_PLAN_MD, name="fix-plan")
    block = plan_mode_reminder_block(full=True, plan_file="/p/x.md", exists=True, playbook=pb)
    assert "operator playbook" in block.lower()
    assert "1. " in block and "2. " in block and "3. " in block


# --------------------------------------------------------------------------- #
# (3b) REUSE — resolve a saved-plan artifact into an active playbook          #
# --------------------------------------------------------------------------- #


def test_resolve_saved_plan_playbook_from_registry(tmp_path: Path) -> None:
    app = _plan_app(tmp_path)
    sess, plan_file = _plan_session(app, tmp_path)
    ref = save_approved_plan(app, sess.id, plan_file=str(plan_file))

    pb = resolve_saved_plan_playbook(app, "ws_default", ref["name"])
    assert pb is not None
    assert len(pb.steps) == 3


def test_resolve_dangling_ref_is_typed_reject(tmp_path: Path) -> None:
    app = _plan_app(tmp_path)
    with pytest.raises(PlanReuseError) as exc:
        resolve_saved_plan_playbook(app, "ws_default", "no-such-saved-plan")
    assert exc.value.reason == "dangling_plan_ref"


def _write_skill(ws: Path, skill_id: str, frontmatter: str, body: str) -> None:
    d = ws / ".claude" / "skills" / skill_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {skill_id}\n{frontmatter}---\n\n{body}\n", encoding="utf-8"
    )


def _invoke(app: Any, sid: str, ws: Path, skill_id: str, *, agent_id: str = "main") -> str:
    agent = AgentDef(id=agent_id, source="expert_pack", title="A", skills=[skill_id], metadata={})
    catalog = SkillCatalog(home=ws / "no-home", cwd=ws)
    rt = SkillRuntime(resolutions=catalog.resolve_declared([skill_id]))
    tool = build_load_skill_tool(agent, rt)
    tok_a = _ctx.set_app(app)
    tok_s = _ctx.set_session_id(sid)
    try:
        return str(tool.func(skill_id=skill_id))
    finally:
        _ctx.reset(tok_s)
        _ctx.reset(tok_a)


def test_skill_playbook_by_reference_activates(tmp_path: Path) -> None:
    """A plan-entering skill declaring playbook_from_plan resolves the saved plan + activates it."""
    app = _plan_app(tmp_path)
    # Save a plan artifact first (so the reference resolves).
    sess, plan_file = _plan_session(app, tmp_path)
    ref = save_approved_plan(app, sess.id, plan_file=str(plan_file))
    saved_name = ref["name"]

    ws = tmp_path  # workspace root; the skill lives under ws/.claude/skills
    _write_skill(
        ws, "replay-plan", f"effect: plan_workflow\nplaybook_from_plan: {saved_name}\n", "BODY."
    )
    consumer = app.state.sessions.create(workspace_id="ws_default", title="t2", mode="edit")

    out = _invoke(app, consumer.id, ws, "replay-plan")

    fresh = app.state.sessions.get(consumer.id)
    assert fresh.mode == "plan"  # entered plan mode
    pb = recorded_playbook(fresh)
    assert pb is not None and len(pb.steps) == 3  # the saved plan's derived skeleton is active
    assert "BODY." in out


def test_record_plan_playbook_dangling_ref_typed(tmp_path: Path) -> None:
    app = _plan_app(tmp_path)
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="plan")
    ref = SimpleNamespace(id="replay", meta={"playbook_from_plan": "no-such-plan"})
    with pytest.raises(PlanReuseError) as exc:
        record_plan_playbook(app, sess.id, ref, None, default_name="replay")
    assert exc.value.reason == "dangling_plan_ref"


# --------------------------------------------------------------------------- #
# (5) byte-identical when no saved plan / no playbook                         #
# --------------------------------------------------------------------------- #


def test_reminder_byte_identical_without_playbook(tmp_path: Path) -> None:
    """A plan session with no saved plan/playbook reminds byte-for-byte as before P1.6c."""
    app = _plan_app(tmp_path)
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="plan")
    plan_file = tmp_path / "t.md"
    plan_file.write_text("# t\n", encoding="utf-8")
    app.state.sessions.update(sess.id, metadata_patch={"plan_file": str(plan_file)})

    got = inject_plan_mode_reminder(app, sess.id, app.state.sessions.get(sess.id), "hello")
    # The composed block equals the direct default composition (no playbook injected).
    expected_block = plan_mode_reminder_block(
        full=True, plan_file=str(plan_file), exists=True, playbook=None
    )
    assert got.startswith(expected_block)


def test_record_plan_playbook_noop_without_ref(tmp_path: Path) -> None:
    """record_plan_playbook is a strict no-op when neither an inline playbook nor a ref is present."""
    app = _plan_app(tmp_path)
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="plan")
    ref = SimpleNamespace(id="plain", meta={})
    record_plan_playbook(app, sess.id, ref, None, default_name="plain")
    assert recorded_playbook(app.state.sessions.get(sess.id)) is None


def test_record_plan_playbook_inline_delegates(tmp_path: Path) -> None:
    """An inline playbook records identically to the P1.6b path (behavior preserved)."""
    from clio_agent.gact.planning import Playbook, PlaybookStep

    app = _plan_app(tmp_path)
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="plan")
    ref = SimpleNamespace(id="ir", meta={})
    pb = Playbook(name="", steps=(PlaybookStep(name="Triage"),))
    record_plan_playbook(app, sess.id, ref, pb, default_name="ir")
    got = recorded_playbook(app.state.sessions.get(sess.id))
    assert got is not None
    assert [s.name for s in got.steps] == ["Triage"]
    assert got.name == "ir"  # unnamed playbook stamped with the skill id (P1.6b behavior)


# --------------------------------------------------------------------------- #
# residual hardening (reviewer follow-ups, same slice):                       #
# playbook_from_plan placement is TYPED at parse time (never silently         #
# ignored) and the by-content register channel (plan file OUTSIDE the         #
# workspace root — the realistic plans_dir topology) is pinned.               #
# --------------------------------------------------------------------------- #


def test_plan_ref_on_effectless_skill_is_typed_reject() -> None:
    """``playbook_from_plan`` on an effect-LESS skill is misplaced -> typed reject, never dropped."""
    from clio_agent.gact.agents.skill_effects import SkillEffectError, parse_skill_effect

    with pytest.raises(SkillEffectError) as exc:
        parse_skill_effect({"playbook_from_plan": "release-plan"})
    assert exc.value.reason == "playbook_requires_plan_mode"


def test_plan_ref_on_non_plan_effect_is_typed_reject() -> None:
    """``playbook_from_plan`` beside a NON-plan-entering effect -> typed reject, never dropped."""
    from clio_agent.gact.agents.skill_effects import SkillEffectError, parse_skill_effect

    with pytest.raises(SkillEffectError) as exc:
        parse_skill_effect(
            {"effect": {"kind": "enter_mode", "mode": "architect"}, "playbook_from_plan": "x"}
        )
    assert exc.value.reason == "playbook_requires_plan_mode"


def test_plan_ref_beside_inline_playbook_is_typed_reject() -> None:
    """Declaring BOTH an inline playbook and a saved-plan reference is ambiguous -> typed reject."""
    from clio_agent.gact.agents.skill_effects import SkillEffectError, parse_skill_effect

    with pytest.raises(SkillEffectError) as exc:
        parse_skill_effect(
            {
                "effect": {"kind": "plan_workflow"},
                "playbook": '[{"name": "Step"}]',
                "playbook_from_plan": "release-plan",
            }
        )
    assert exc.value.reason == "conflicting_playbook_declarations"


def test_save_approved_plan_content_channel_outside_workspace(tmp_path: Path) -> None:
    """A plan file OUTSIDE the workspace root saves via the inline-content channel.

    This is the realistic topology: ``plan_acl.plans_dir()`` is cwd-relative while the
    session workspace root can be elsewhere. The path channel would reject the escape, so
    ``save_approved_plan`` must fall through to inline content — and still round-trip.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    app = _plan_app(ws)
    # Save runs after plan-exit flips the mode; mirror that (plan mode denies content writes).
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    plan_file = outside / "fix-it.md"
    plan_file.write_text(_PLAN_MD, encoding="utf-8")

    ref = save_approved_plan(app, sess.id, plan_file=str(plan_file))

    assert ref["saved"] is True, f"content-channel save failed: {ref!r}"
    derived = resolve_saved_plan_playbook(app, "ws_default", ref["name"], name=ref["name"])
    assert derived.steps  # the saved content round-trips into a usable skeleton
