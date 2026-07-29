"""P1.6b #1068 — operator playbooks (CUGA-style plan skeletons).

An operator supplies a declarative plan SKELETON via a skill that carries a planning effect:
an ordered list of named steps, each optionally with guidance text and a ``tools_allowed``
list. The skeleton is parsed from the skill's TRUSTED frontmatter (never the body), recorded
on ``session.metadata`` (no fifth store, like ``plan_variant``), presented by the plan-mode
reminder as the required plan structure, and its per-step ``tools_allowed`` NARROWS the
grant_resolver resolution (tighten-only). These tests pin that contract:

  (1) skeleton parsed from a skill frontmatter + recorded on session.metadata; malformed
      declarations get a TYPED rejection (never a silent drop);
  (2) the plan-mode reminder presents the skeleton when a playbook is active, winning over the
      variant structure hint;
  (3) per-step ``tools_allowed`` NARROWS resolution — a tool outside the step allowlist
      resolves ``deny`` while a matching one still resolves ``allow`` — and is TIGHTEN-ONLY
      (never grants over a user deny);
  (4) a no-playbook session resolves + reminds BYTE-IDENTICALLY to P1.6a;
  (5) the active playbook CLEARS on plan_exit approve (symmetry with the variant tag).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents.skill_effects import SkillEffectError, parse_skill_effect
from clio_agent.gact.agents.skill_runtime import SkillRuntime, build_load_skill_tool
from clio_agent.gact.app import (
    _make_permission_gate,
    _tool_session_context,
    build_app,
)
from clio_agent.gact.permission_gate import _policy_action_for_tool
from clio_agent.gact.plan_mode import (
    inject_plan_mode_reminder,
    resolve_plan_exit_answer,
)
from clio_agent.gact.planning import (
    PLAN_VARIANT_METADATA_KEY,
    PLAN_VARIANT_WORKFLOW,
    Playbook,
    PlaybookStep,
    active_playbook_allowed_tools,
    parse_playbook,
    record_playbook,
    recorded_playbook,
)
from clio_agent.gact.runtime.grant_resolver import resolve
from clio_agent.gact.skills import SkillCatalog
from clio_agent.gact.types import AgentDef

pytestmark = pytest.mark.usefixtures("host_agent_executor")

_USER_TEXT = "investigate the failing test"

# A single-line JSON playbook (the trusted frontmatter encoding — the hand-rolled SKILL.md
# frontmatter parser stores the value verbatim, so structured playbooks ride as JSON).
_PLAYBOOK_JSON = (
    '[{"name": "Triage", "guidance": "gather evidence", "tools_allowed": ["fs_read_file"]}, '
    '{"name": "Fix", "guidance": "apply the change"}]'
)


# --------------------------------------------------------------------------- #
# skill fixture helpers (mirroring tests/test_gact/test_skill_effects.py)      #
# --------------------------------------------------------------------------- #


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


def _plan_session(tmp_path: Path, *, variant: str = "", mode: str = "plan"):
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode=mode)
    if variant:
        app.state.sessions.update(sess.id, metadata_patch={PLAN_VARIANT_METADATA_KEY: variant})
    return app, app.state.sessions.get(sess.id)


# --------------------------------------------------------------------------- #
# (1) parse + record; typed reject on malformed                               #
# --------------------------------------------------------------------------- #


def test_parse_playbook_from_json_string() -> None:
    pb = parse_playbook(_PLAYBOOK_JSON)
    assert pb is not None
    assert [s.name for s in pb.steps] == ["Triage", "Fix"]
    assert pb.steps[0].tools_allowed == ("fs_read_file",)
    assert pb.steps[0].guidance == "gather evidence"
    assert pb.steps[1].tools_allowed == ()  # optional — absent is empty


def test_parse_playbook_from_list_form() -> None:
    pb = parse_playbook([{"name": "A", "tools_allowed": ["x", "y"]}])
    assert pb is not None
    assert pb.steps[0].tools_allowed == ("x", "y")


def test_parse_playbook_none_and_blank_are_none() -> None:
    assert parse_playbook(None) is None
    assert parse_playbook("") is None
    assert parse_playbook("   ") is None


def test_parse_playbook_malformed_json_is_typed_error() -> None:
    with pytest.raises(SkillEffectError) as exc:
        parse_skill_effect({"effect": "plan_workflow", "playbook": "{not json"})
    assert exc.value.reason == "malformed_playbook"


def test_parse_playbook_unknown_step_field_is_typed_error() -> None:
    with pytest.raises(SkillEffectError) as exc:
        parse_skill_effect(
            {"effect": "plan_workflow", "playbook": '[{"name": "a", "frobnicate": "x"}]'}
        )
    assert exc.value.reason == "unknown_playbook_field"


def test_parse_playbook_step_without_name_is_typed_error() -> None:
    with pytest.raises(SkillEffectError) as exc:
        parse_skill_effect({"effect": "plan_workflow", "playbook": '[{"guidance": "x"}]'})
    assert exc.value.reason == "playbook_step_missing_name"


def test_playbook_on_non_plan_effect_is_typed_error() -> None:
    """A playbook is a PLAN skeleton — declaring it beside a non-plan effect is rejected."""
    with pytest.raises(SkillEffectError) as exc:
        parse_skill_effect(
            {"effect": "loop", "effect_max_iters": "2", "playbook": '[{"name": "a"}]'}
        )
    assert exc.value.reason == "playbook_requires_plan_mode"


def test_playbook_on_effectless_skill_wellformed_is_typed_error() -> None:
    """A well-formed playbook declared on a skill with NO effect is MISPLACED — a playbook only
    rides a plan-entering effect. It must be a typed reject, never a silent drop (Blocker 3)."""
    with pytest.raises(SkillEffectError) as exc:
        parse_skill_effect({"playbook": _PLAYBOOK_JSON})
    assert exc.value.reason == "playbook_requires_plan_mode"


def test_playbook_on_effectless_skill_malformed_is_typed_error() -> None:
    """Even a MALFORMED playbook on an effect-less skill is a typed reject, not a silent drop."""
    with pytest.raises(SkillEffectError) as exc:
        parse_skill_effect({"playbook": "{not json"})
    assert exc.value.reason == "malformed_playbook"


def test_effectless_skill_without_playbook_is_none() -> None:
    """The common case is untouched: no effect and no playbook parses to None (backward-compatible)."""
    assert parse_skill_effect({"name": "x"}) is None
    assert parse_skill_effect({"playbook": ""}) is None
    assert parse_skill_effect({"playbook": "   "}) is None


def test_playbook_parsed_on_plan_effect() -> None:
    eff = parse_skill_effect(
        {"effect": "enter_mode", "effect_mode": "plan", "playbook": _PLAYBOOK_JSON}
    )
    assert eff is not None
    assert eff.playbook is not None
    assert [s.name for s in eff.playbook.steps] == ["Triage", "Fix"]


def test_playbook_recorded_on_skill_invocation(tmp_path: Path) -> None:
    """Invoking a skill that declares a playbook records the ACTIVE playbook on session.metadata."""
    ws = tmp_path / "ws"
    _write_skill(ws, "ir", f"effect: plan_workflow\nplaybook: {_PLAYBOOK_JSON}\n", "IR_BODY.")
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")

    out = _invoke(app, sess.id, ws, "ir")

    fresh = app.state.sessions.get(sess.id)
    assert fresh.mode == "plan"
    pb = recorded_playbook(fresh)
    assert pb is not None
    assert [s.name for s in pb.steps] == ["Triage", "Fix"]
    assert "IR_BODY." in out  # the body still loads


def test_malformed_playbook_skill_rejected_on_invocation(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _write_skill(ws, "bad-pb", "effect: plan_workflow\nplaybook: {not json\n", "BODY.")
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    with pytest.raises(SkillEffectError) as exc:
        _invoke(app, sess.id, ws, "bad-pb")
    assert exc.value.reason == "malformed_playbook"
    # Nothing armed: mode unchanged (the reject fired before the transition).
    assert app.state.sessions.get(sess.id).mode == "edit"


# --------------------------------------------------------------------------- #
# (2) the reminder presents the skeleton when active                          #
# --------------------------------------------------------------------------- #


def test_reminder_presents_playbook_skeleton(tmp_path: Path) -> None:
    app, sess = _plan_session(tmp_path)
    record_playbook(app, sess.id, parse_playbook(_PLAYBOOK_JSON))
    out = inject_plan_mode_reminder(app, sess.id, app.state.sessions.get(sess.id), _USER_TEXT)
    assert "playbook" in out.lower()
    assert "1. Triage" in out
    assert "2. Fix" in out
    assert "gather evidence" in out  # per-step guidance surfaced
    assert "fs_read_file" in out  # per-step tools_allowed surfaced
    assert out.endswith(_USER_TEXT)


def test_playbook_wins_over_variant_structure_hint(tmp_path: Path) -> None:
    """The playbook skeleton REPLACES the variant's structure hint (documented precedence),
    while the variant's extra plan sections still compose."""
    app, sess = _plan_session(tmp_path, variant=PLAN_VARIANT_WORKFLOW)
    record_playbook(app, sess.id, parse_playbook(_PLAYBOOK_JSON))
    out = inject_plan_mode_reminder(app, sess.id, app.state.sessions.get(sess.id), _USER_TEXT)
    # Playbook present; the workflow's generic "numbered implementation workflow" structure hint
    # is replaced by the skeleton.
    assert "1. Triage" in out
    assert "numbered implementation workflow" not in out
    # The workflow variant's extra section still composes.
    assert "Risks & Dependencies" in out


# --------------------------------------------------------------------------- #
# (3) tools_allowed NARROWS resolution (tighten-only)                         #
# --------------------------------------------------------------------------- #

_ALLOW_ALL = [
    {
        "kind": "tool",
        "action": "allow",
        "tool_name_pattern": "*",
        "scope": "session",
        "scope_id": "s1",
    }
]


def test_playbook_narrows_out_of_allowlist_to_deny() -> None:
    allowed = ("fs_read_file", "web_fetch")
    # a tool IN the step allowlist keeps the user allow (no playbook narrowing for it)
    assert (
        resolve("tool", "web_fetch", policies=_ALLOW_ALL, session_id="s1", playbook_allowed=allowed)
        == "allow"
    )
    # a tool OUTSIDE the allowlist is narrowed to deny even though the user allowed "*"
    assert (
        resolve(
            "tool", "shell_bash", policies=_ALLOW_ALL, session_id="s1", playbook_allowed=allowed
        )
        == "deny"
    )


def test_playbook_is_tighten_only_never_grants() -> None:
    """A tool IN the allowlist that a user policy DENIES stays denied — a playbook only
    narrows, it never grants."""
    policies = [
        {
            "kind": "tool",
            "action": "deny",
            "tool_name_pattern": "shell_bash",
            "scope": "session",
            "scope_id": "s1",
        }
    ]
    assert (
        resolve(
            "tool",
            "shell_bash",
            policies=policies,
            session_id="s1",
            playbook_allowed=("shell_bash",),
        )
        == "deny"
    )


def test_playbook_empty_allowlist_imposes_no_narrowing() -> None:
    """A step with no tools_allowed imposes no narrowing (optional per-step field)."""
    assert (
        resolve("tool", "shell_bash", policies=_ALLOW_ALL, session_id="s1", playbook_allowed=())
        == "allow"
    )


def test_active_playbook_allowed_tools_reads_active_step(tmp_path: Path) -> None:
    app, sess = _plan_session(tmp_path)
    record_playbook(app, sess.id, parse_playbook(_PLAYBOOK_JSON))
    fresh = app.state.sessions.get(sess.id)
    # active step defaults to the first — its allowlist is surfaced.
    assert active_playbook_allowed_tools(fresh) == ("fs_read_file",)
    # no playbook -> None (no narrowing)
    _, plain = _plan_session(tmp_path / "b")
    assert active_playbook_allowed_tools(plain) is None


def test_playbook_narrows_plan_safe_tool_live_through_gate(tmp_path: Path) -> None:
    """NON-VACUOUS enforcement (Blocker 1+2): in plan mode a plan-SAFE tool (web_fetch) that the
    plan-ACL @50 allow-band WOULD allow is DENIED when the active playbook step's allowlist omits
    it. The deny is caused SOLELY by the playbook narrowing — with NO playbook the same call is
    ALLOWED — so removing either the resolver consult or the gate wiring flips it back to allow and
    turns this test red. The plan-mode lock alone cannot produce this deny (web_fetch is @50 allowed).
    """
    # Control: with NO playbook active, web_fetch resolves ALLOW in plan mode (the @50 allow-band).
    plain_app, plain = _plan_session(tmp_path / "plain")
    assert (
        _policy_action_for_tool(
            plain_app,
            session_id=plain.id,
            session=plain,
            tool_name="web_fetch",
            args={},
            mode="plan",
        )
        == "allow"
    )

    # With a playbook whose active step allows only fs_read_file, web_fetch is narrowed to DENY —
    # live through the gate shim (active_playbook_allowed_tools -> resolve).
    app, sess = _plan_session(tmp_path)
    step = PlaybookStep(name="triage", tools_allowed=("fs_read_file",))
    record_playbook(app, sess.id, Playbook(name="ir", steps=(step,)))
    fresh = app.state.sessions.get(sess.id)
    assert (
        _policy_action_for_tool(
            app,
            session_id=sess.id,
            session=fresh,
            tool_name="web_fetch",
            args={},
            mode="plan",
        )
        == "deny"
    )


def test_playbook_narrows_web_fetch_live_through_make_permission_gate(tmp_path: Path) -> None:
    """PRIMARY runtime enforcement path (Blocker 1 repair): pins the narrowing through the REAL
    interactive gate closure built by :func:`_make_permission_gate` — the one the tool executor
    calls for EVERY model tool call (its main policy match at permission_gate.py:649 consults
    :func:`_policy_detail_for_tool`), NOT just the ``_policy_action_for_tool`` shim exercised above.

    In plan mode a plan-SAFE tool (``web_fetch``, which the plan-ACL @50 allow-band WOULD allow) is
    DENIED when the active step's allowlist omits it, and ALLOWED with no playbook. The deny is
    caused SOLELY by the playbook narrowing — the plan-mode lock alone cannot produce it (web_fetch
    is @50 allowed) — so severing the playbook wiring on the detail shim
    (``playbook_allowed=...`` at permission_gate.py:289, the shim this closure consults) flips the
    deny back to allow and turns this test red. Anti-lockout: ``plan_exit`` stays allowed even
    though the active step allowlist omits it.
    """
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t", "mode": "plan"}).json()["id"]
        gate = _make_permission_gate(app)

        # Control: with NO playbook active, the live gate ALLOWS web_fetch (the @50 allow-band).
        with _tool_session_context(sid):
            assert gate("web_fetch", {}) == "allow"

        # Arm a playbook whose active step allows only fs_read_file.
        step = PlaybookStep(name="triage", tools_allowed=("fs_read_file",))
        record_playbook(app, sid, Playbook(name="ir", steps=(step,)))

        # The SAME live gate now DENIES web_fetch — solely because the playbook narrows it.
        with _tool_session_context(sid):
            assert gate("web_fetch", {}) == "deny"

        # Anti-lockout exemption survives the narrowing: plan_exit is still allowed.
        with _tool_session_context(sid):
            assert gate("plan_exit", {}) == "allow"


def test_playbook_never_strands_plan_exit_or_plan_file(tmp_path: Path) -> None:
    """The playbook narrowing composes with plan mode WITHOUT stranding it: plan_exit is exempt
    (anti-lockout — a playbook can never lock the model in plan mode) and the sole plan-file write
    carve-out (@70) still outranks the playbook deny, even though the active step allows neither."""
    app, sess = _plan_session(tmp_path)
    step = PlaybookStep(name="triage", tools_allowed=("fs_read_file",))
    record_playbook(app, sess.id, Playbook(name="ir", steps=(step,)))
    # inject once so the deterministic plan-file path is recorded.
    inject_plan_mode_reminder(app, sess.id, app.state.sessions.get(sess.id), _USER_TEXT)
    fresh = app.state.sessions.get(sess.id)
    plan_file = fresh.metadata["plan_file"]

    # plan_exit stays allowed despite being absent from the step allowlist (anti-lockout exemption).
    assert (
        _policy_action_for_tool(
            app, session_id=sess.id, session=fresh, tool_name="plan_exit", args={}, mode="plan"
        )
        == "allow"
    )
    # the plan-file write still resolves allow — the @70 carve-out outranks the playbook deny
    # so plan mode is never stranded (composition).
    assert (
        _policy_action_for_tool(
            app,
            session_id=sess.id,
            session=fresh,
            tool_name="fs_apply_edit_write",
            args={"path": plan_file},
            mode="plan",
        )
        == "allow"
    )


# --------------------------------------------------------------------------- #
# (4) no-playbook session is byte-identical to P1.6a                          #
# --------------------------------------------------------------------------- #


def test_no_playbook_resolve_is_byte_identical() -> None:
    baseline = resolve("tool", "shell_bash", policies=_ALLOW_ALL, session_id="s1")
    with_param = resolve(
        "tool", "shell_bash", policies=_ALLOW_ALL, session_id="s1", playbook_allowed=None
    )
    assert baseline == with_param == "allow"


def test_no_playbook_reminder_is_unchanged(tmp_path: Path) -> None:
    app, sess = _plan_session(tmp_path)
    out = inject_plan_mode_reminder(app, sess.id, app.state.sessions.get(sess.id), _USER_TEXT)
    assert "playbook" not in out.lower()
    assert recorded_playbook(app.state.sessions.get(sess.id)) is None


# --------------------------------------------------------------------------- #
# (5) the playbook CLEARS on plan_exit approve                                #
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


def test_playbook_cleared_after_plan_exit_approve(tmp_path: Path) -> None:
    app, sess = _plan_session(tmp_path)
    record_playbook(app, sess.id, parse_playbook(_PLAYBOOK_JSON))
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n", encoding="utf-8")
    app.state.sessions.update(sess.id, metadata_patch={"plan_file": str(plan_file)})
    assert recorded_playbook(app.state.sessions.get(sess.id)) is not None

    q = _pending_plan_exit_question(app, app.state.sessions.get(sess.id), plan_file=str(plan_file))
    resolve_plan_exit_answer(app, _fake_deps(), sess.id, _answer(q, selected=["auto"]))

    fresh = app.state.sessions.get(sess.id)
    assert fresh.mode == "edit"
    assert recorded_playbook(fresh) is None  # playbook cleared on leaving plan mode


def test_playbook_preserved_after_plan_exit_reject(tmp_path: Path) -> None:
    app, sess = _plan_session(tmp_path)
    record_playbook(app, sess.id, parse_playbook(_PLAYBOOK_JSON))
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n", encoding="utf-8")
    app.state.sessions.update(sess.id, metadata_patch={"plan_file": str(plan_file)})

    q = _pending_plan_exit_question(app, app.state.sessions.get(sess.id), plan_file=str(plan_file))
    resolve_plan_exit_answer(
        app, _fake_deps(), sess.id, _answer(q, selected=["reject"], answer="tighten step 2")
    )

    fresh = app.state.sessions.get(sess.id)
    assert fresh.mode == "plan"  # stayed in plan mode
    assert recorded_playbook(fresh) is not None  # playbook preserved for the revision turn
