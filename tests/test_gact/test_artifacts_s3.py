"""Unit tests for the S3 agent-proposed artifacts floor (#966/#969).

Covers the ``create_artifact`` promotion path — the acceptance/rejection matrix
(missing path, escape-root, over-cap boundary, duplicate no-op, kind validation
incl. the RESERVED ``plan``), the harness-hashes-not-the-model invariant (a
model-supplied sha in the args is IGNORED), inline-content-lands-as-a-file, the
batch summary, tool injection presence, and the wholesale deletion of the inert
``artifacts`` structured-output field from builders/signatures (grep-lock,
baseline-0 style).

Each key lock carries a sabotage note: the referenced neutralization turns the
named assertion red, proving the test binds the invariant (not a tautology).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.artifacts import proposals as P
from clio_agent.gact.artifacts.minting import get_registry
from clio_agent.gact.artifacts.proposals import (
    Proposal,
    RejectionReason,
    build_create_artifact_tool,
    parse_proposals,
    promote_proposal,
    promote_proposals,
    validate_kind,
)
from clio_agent.gact.artifacts.records import ArtifactKind, Custody, Mechanism
from clio_agent.gact.sessions import SessionStore

# --------------------------------------------------------------------------- #
# Fakes: a minimal app whose state carries just what mint/emit/proposals read.
# --------------------------------------------------------------------------- #


class _CapturingArc:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def record_semantic_event(self, event: Any) -> Any:
        self.events.append(event)
        return event


class _FakeWorkspaces:
    def __init__(self, roots: dict[str, str]) -> None:
        self._roots = roots

    def get(self, wid: str) -> Any:
        root = self._roots.get(wid)
        return SimpleNamespace(root_path=root) if root else None


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def publish(self, event: Any) -> None:
        self.events.append(event)


def _make_app(
    tmp_path: Path,
    *,
    workspace_root: Path | None = None,
    mode: str = "chat",
    policies: list[dict[str, Any]] | None = None,
):
    store = SessionStore(path=tmp_path / "sessions.json")
    sess = store.create(workspace_id="ws1", title="t", mode=mode)
    arc = _CapturingArc()
    root = workspace_root if workspace_root is not None else tmp_path
    state = SimpleNamespace(
        sessions=store,
        arc=arc,
        workspaces=_FakeWorkspaces({"ws1": str(root)}),
        semantic_event_sink=object(),
        semantic_trace_detail_level="semantic",
        semantic_trace_backend=None,
        artifact_registry=None,
        turn_artifacts={},
        artifact_proposal_counts={},
        # Permission machinery (finding [2]): the inline-content write consults the
        # same policy layer + audit ledger every other write path does.
        permissions={},
        permission_events={},
        permission_policies=list(policies or []),
        bus=_FakeBus(),
    )
    return SimpleNamespace(state=state), sess, arc


def _proposal_events(arc: _CapturingArc) -> list[Any]:
    return [e for e in arc.events if getattr(e, "event_type", "") == "artifact.proposed"]


# --------------------------------------------------------------------------- #
# Kind validation
# --------------------------------------------------------------------------- #


def test_validate_kind_maps_enum_defaults_and_rejects():
    # Sabotage: default an empty kind to REPORT instead of OTHER -> red.
    assert validate_kind("") == ArtifactKind.OTHER
    assert validate_kind("report") == ArtifactKind.REPORT
    assert validate_kind("IMAGE") == ArtifactKind.IMAGE
    with pytest.raises(ValueError, match="reserved"):
        validate_kind("plan")  # RESERVED (#966.4) — nothing designates it
    with pytest.raises(ValueError, match="unknown kind"):
        validate_kind("bogus")


def test_reserved_plan_kind_rejected_as_typed_invalid_kind(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    f = tmp_path / "p.md"
    f.write_text("x", encoding="utf-8")
    out = promote_proposal(app, sess.id, Proposal(path=str(f), kind="plan"), workspace_id="ws1")
    # Sabotage: let validate_kind pass plan through -> accepted, this reddens.
    assert out.accepted is False
    assert out.reason == RejectionReason.INVALID_KIND.value
    assert _proposal_events(arc), "a rejected proposal must still emit an event"


# --------------------------------------------------------------------------- #
# Acceptance (path-based) — mechanism=model, harness hash, quarantined intent
# --------------------------------------------------------------------------- #


def test_accept_existing_path_mints_model_designation(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    report = tmp_path / "analysis.md"
    body = "# Findings\nthe answer\n"
    report.write_text(body, encoding="utf-8")

    out = promote_proposal(
        app,
        sess.id,
        Proposal(path=str(report), kind="report", annotation="the deliverable"),
        workspace_id="ws1",
    )
    assert out.accepted and out.created
    ver = out.version
    assert ver is not None
    # mechanism=model → designation=agent-proposed (owner #966.5)
    assert ver.mechanism == Mechanism.MODEL
    assert ver.producer.get("designation") == "agent-proposed"
    # S6 (#972): a small model-designated deliverable is ingested into CAS at mint —
    # its bytes now survive workspace churn (custody `cas`, not workspace-referenced).
    assert ver.custody == Custody.CAS
    assert ver.kind == ArtifactKind.REPORT
    # HARNESS hash, not the model's claim.
    assert ver.sha256 == hashlib.sha256(report.read_bytes()).hexdigest()
    # Model intent is quarantined in annotation, never merged into evidence.
    assert ver.annotation == "the deliverable"
    # Registered in the projection.
    rec = get_registry(app).get("ws1", "analysis.md")
    assert rec is not None and rec.head is ver


def test_model_supplied_hash_is_ignored_harness_wins(tmp_path):
    app, sess, _ = _make_app(tmp_path)
    report = tmp_path / "r.md"
    report.write_text("real bytes", encoding="utf-8")
    real = hashlib.sha256(report.read_bytes()).hexdigest()
    fake = "0" * 64
    # Feed a POPULATED model sha256 through the exact args path a batch tool call
    # takes (finding [10]): parse_proposals -> promote_proposals. The harness MUST
    # override it. Assert on the RECORDED evidence hash the registry stores/serves —
    # proving the harness value wins, not merely that the parse layer dropped the key.
    # Sabotage: carry a proposal-supplied sha into evidence -> the recorded hash
    # below flips to `fake` and this reddens.
    result = promote_proposals(
        app,
        sess.id,
        parse_proposals(
            name="",
            kind="",
            path="",
            content="",
            annotation="",
            artifacts=[{"path": str(report), "kind": "report", "sha256": fake, "hash": fake}],
        ),
        workspace_id="ws1",
    )
    assert result["accepted"] == 1
    rec = get_registry(app).get("ws1", "r.md")
    assert rec is not None and rec.head is not None
    recorded = rec.head.sha256
    assert recorded == real
    assert recorded != fake


def test_proposal_from_mapping_has_no_hash_field():
    # The parsed proposal must not even carry a model-supplied hash.
    p = Proposal.from_mapping({"path": "/x", "sha256": "deadbeef", "digest": "z"})
    assert not hasattr(p, "sha256")
    assert p.path == "/x"


# --------------------------------------------------------------------------- #
# Rejections: missing path, escape-root, missing input
# --------------------------------------------------------------------------- #


def test_reject_missing_path(tmp_path):
    app, sess, _ = _make_app(tmp_path)
    out = promote_proposal(
        app, sess.id, Proposal(path=str(tmp_path / "nope.md"), kind="report"), workspace_id="ws1"
    )
    assert out.accepted is False
    assert out.reason == RejectionReason.PATH_MISSING.value


def test_reject_escape_root(tmp_path):
    app, sess, _ = _make_app(tmp_path, workspace_root=tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    # Sabotage: drop the _contained check -> path_missing/accepted, this reddens.
    out = promote_proposal(
        app, sess.id, Proposal(path=str(outside), kind="report"), workspace_id="ws1"
    )
    assert out.accepted is False
    assert out.reason == RejectionReason.ESCAPES_ROOT.value


def test_reject_missing_input(tmp_path):
    app, sess, _ = _make_app(tmp_path)
    out = promote_proposal(app, sess.id, Proposal(kind="report"), workspace_id="ws1")
    assert out.accepted is False
    assert out.reason == RejectionReason.MISSING_INPUT.value


def test_reject_containment_unresolved(tmp_path):
    app, sess, _ = _make_app(tmp_path)
    f = tmp_path / "r.md"
    f.write_text("x", encoding="utf-8")
    # Unknown workspace id → root unresolvable → typed reject (never reads the path).
    out = promote_proposal(app, sess.id, Proposal(path=str(f), kind="report"), workspace_id="ghost")
    assert out.accepted is False
    assert out.reason == RejectionReason.CONTAINMENT_UNRESOLVED.value


# --------------------------------------------------------------------------- #
# Duplicate: same name + same sha → existing record, created=False (not an error)
# --------------------------------------------------------------------------- #


def test_duplicate_returns_existing_created_false(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    report = tmp_path / "d.md"
    report.write_text("same bytes", encoding="utf-8")
    first = promote_proposal(
        app, sess.id, Proposal(path=str(report), kind="report"), workspace_id="ws1"
    )
    assert first.accepted and first.created
    second = promote_proposal(
        app, sess.id, Proposal(path=str(report), kind="report"), workspace_id="ws1"
    )
    # Sabotage: raise on a dup instead of returning existing -> this reddens.
    assert second.accepted is True
    assert second.created is False
    assert second.reason == "already_registered"
    assert second.version is not None
    assert second.version.artifact_id == first.version.artifact_id
    # Exactly ONE version in the chain — the dup minted nothing new.
    rec = get_registry(app).get("ws1", "d.md")
    assert len(rec.versions) == 1


# --------------------------------------------------------------------------- #
# Per-turn promotion cap (boundary) — only NEW promotions consume budget
# --------------------------------------------------------------------------- #


def test_over_cap_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "proposals_per_turn", lambda: 2)
    app, sess, _ = _make_app(tmp_path)
    outs = []
    for i in range(3):
        f = tmp_path / f"a{i}.md"
        f.write_text(f"body {i}", encoding="utf-8")
        outs.append(
            promote_proposal(
                app, sess.id, Proposal(path=str(f), kind="report"), workspace_id="ws1", turn_id="t1"
            )
        )
    # First two accepted; the third trips the cap (boundary).
    assert outs[0].accepted and outs[0].created
    assert outs[1].accepted and outs[1].created
    # Sabotage: increment the counter on dedup too, or check >cap not >=cap -> shifts.
    assert outs[2].accepted is False
    assert outs[2].reason == RejectionReason.OVER_CAP.value
    assert P.proposal_count(app, sess.id, "t1") == 2


def test_duplicate_does_not_consume_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "proposals_per_turn", lambda: 1)
    app, sess, _ = _make_app(tmp_path)
    f = tmp_path / "x.md"
    f.write_text("bytes", encoding="utf-8")
    a = promote_proposal(
        app, sess.id, Proposal(path=str(f), kind="report"), workspace_id="ws1", turn_id="t1"
    )
    b = promote_proposal(
        app, sess.id, Proposal(path=str(f), kind="report"), workspace_id="ws1", turn_id="t1"
    )
    # The re-designation of identical bytes is a no-op that never burns budget.
    assert a.accepted and a.created
    assert b.accepted and not b.created
    assert P.proposal_count(app, sess.id, "t1") == 1


# --------------------------------------------------------------------------- #
# Inline content → workspace file + record
# --------------------------------------------------------------------------- #


def test_inline_content_lands_as_file_and_record(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    app, sess, _ = _make_app(tmp_path)
    body = "# In-context report\nauthored this turn\n"
    out = promote_proposal(
        app,
        sess.id,
        Proposal(name="report.md", kind="report", content=body, annotation="my report"),
        workspace_id="ws1",
    )
    assert out.accepted and out.created
    written = tmp_path / "report.md"
    # Sabotage: skip write_text_with_policy -> the file never exists -> red.
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == body
    assert out.version.sha256 == hashlib.sha256(written.read_bytes()).hexdigest()
    assert out.version.annotation == "my report"


def test_inline_content_requires_name(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    app, sess, _ = _make_app(tmp_path)
    out = promote_proposal(app, sess.id, Proposal(kind="report", content="x"), workspace_id="ws1")
    assert out.accepted is False
    assert out.reason == RejectionReason.MISSING_INPUT.value


def test_inline_content_name_cannot_escape_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    app, sess, _ = _make_app(tmp_path, workspace_root=tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    out = promote_proposal(
        app,
        sess.id,
        Proposal(name="../escape.md", kind="report", content="x"),
        workspace_id="ws1",
    )
    assert out.accepted is False
    assert out.reason == RejectionReason.ESCAPES_ROOT.value
    assert not (tmp_path / "escape.md").exists()


# --------------------------------------------------------------------------- #
# Batch form + parsing
# --------------------------------------------------------------------------- #


def test_parse_proposals_batch_vs_single():
    single = parse_proposals(
        name="a", kind="report", path="/p", content="", annotation="", artifacts=None
    )
    assert len(single) == 1 and single[0].path == "/p"
    batch = parse_proposals(
        name="",
        kind="",
        path="",
        content="",
        annotation="",
        artifacts=[
            {"path": "/a", "kind": "report"},
            {"content": "c", "name": "n.md", "kind": "other"},
        ],
    )
    assert len(batch) == 2
    assert batch[0].path == "/a" and batch[1].content == "c"


def test_promote_batch_summary_counts(tmp_path):
    app, sess, _ = _make_app(tmp_path)
    good = tmp_path / "g.md"
    good.write_text("g", encoding="utf-8")
    dup_src = tmp_path / "d.md"
    dup_src.write_text("d", encoding="utf-8")
    promote_proposal(app, sess.id, Proposal(path=str(dup_src), kind="report"), workspace_id="ws1")
    result = promote_proposals(
        app,
        sess.id,
        [
            Proposal(path=str(good), kind="report"),  # new
            Proposal(path=str(dup_src), kind="report"),  # dup
            Proposal(path=str(tmp_path / "missing.md"), kind="report"),  # reject
        ],
        workspace_id="ws1",
    )
    assert result["accepted"] == 1
    assert result["deduplicated"] == 1
    assert result["rejected"] == 1
    assert len(result["artifacts"]) == 3


# --------------------------------------------------------------------------- #
# Tool injection + schema
# --------------------------------------------------------------------------- #


def test_create_artifact_tool_shape():
    agent_def = SimpleNamespace(id="expert-x")
    tool = build_create_artifact_tool(agent_def)
    assert tool.name == "create_artifact"
    # Batch + single-item args are all present on the schema.
    for arg in ("name", "kind", "path", "content", "annotation", "artifacts"):
        assert arg in tool.args


SKILL_BODY = "PROCEDURE_BODY_MARKER"


@pytest.fixture
def skill_pack(tmp_path: Path) -> Path:
    """A pack root shipping one resolvable skill (for the with-skills assertion)."""
    skill_dir = tmp_path / "pack" / "skills" / "quality-rubric"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: quality-rubric\ndescription: Judge data quality\n---\n\n{SKILL_BODY}\n",
        encoding="utf-8",
    )
    return tmp_path / "pack"


def _react_agent(
    pack: Path,
    *,
    skills: list[str],
    source: str = "expert_pack",
    module: dict[str, Any] | None = None,
) -> Any:
    from clio_agent.gact.types import AgentDef

    return AgentDef(
        id="analyst",
        source=source,
        title="Analyst",
        system_prompt="Analyze things.",
        skills=list(skills),
        module=module if module is not None else {"kind": "react"},
        metadata={"pack_definition_path": str(pack / "AGENT.md")},
    )


def _patch_lm(monkeypatch: Any) -> None:
    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object(), raising=True)
    monkeypatch.setattr(
        "clio_agent.config.create_chat_adapter", lambda config: object(), raising=True
    )
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._dynamic_agent_lm_config",
        lambda base_agent, agent_def: SimpleNamespace(
            materialize=lambda cred=None: SimpleNamespace(
                provider="openai", model="m", temperature=0.0
            )
        ),
        raising=True,
    )


def _tool_names(module: Any) -> set[str]:
    return {str(getattr(t, "name", "")) for t in module.tools}


def test_react_branches_attach_create_artifact_unconditionally(skill_pack, monkeypatch):
    """Finding [8]: both react builder branches attach create_artifact REGARDLESS
    of whether a skill resolved — proven on the REAL built module, not a source grep.

    Sabotage: indent the create_artifact append INSIDE the ``if skill_rt.resolved:``
    guard (builders.py, either react site) → the skills-less assertions below go red
    (the tool vanishes when no skill resolves, the exact regression this guards)."""
    from clio_agent.gact.agents.builders import (
        _build_blueprint_dspy_module,
        _build_tool_user_agent_module,
    )

    _patch_lm(monkeypatch)
    base = SimpleNamespace(tool_executor=None)

    # (1) Blueprint react expert with NO resolvable skill: load_skill is absent, but
    # create_artifact MUST still be attached (un-nested from the skill guard).
    bp_noskill = _tool_names(
        _build_blueprint_dspy_module(base, _react_agent(skill_pack, skills=[]))
    )
    assert "create_artifact" in bp_noskill
    assert "load_skill" not in bp_noskill
    # With a resolved skill: BOTH the skill tool AND create_artifact are present.
    bp_skill = _tool_names(
        _build_blueprint_dspy_module(base, _react_agent(skill_pack, skills=["quality-rubric"]))
    )
    assert {"create_artifact", "load_skill"} <= bp_skill

    # (2) The second react site (tool-declaring user agents) honors the same contract.
    tu_noskill = _tool_names(
        _build_tool_user_agent_module(
            base, _react_agent(skill_pack, skills=[], source="user", module={})
        )
    )
    assert "create_artifact" in tu_noskill
    assert "load_skill" not in tu_noskill
    tu_skill = _tool_names(
        _build_tool_user_agent_module(
            base,
            _react_agent(skill_pack, skills=["quality-rubric"], source="user", module={}),
        )
    )
    assert {"create_artifact", "load_skill"} <= tu_skill


# --------------------------------------------------------------------------- #
# Deletion grep-lock (baseline-0): the inert `artifacts` field is GONE
# --------------------------------------------------------------------------- #


def test_builders_no_artifacts_prediction_passthrough():
    src = Path("src/clio_agent/gact/agents/builders.py").read_text(encoding="utf-8")
    # The inert artifacts prediction field (empty-list / empty-str / getattr
    # passthrough) is gone from every dspy.Prediction construction in builders.
    assert re.search(r"\bartifacts\s*=\s*(\[\]|\"\"|getattr)", src) is None


def test_runtime_metadata_tuple_drops_artifacts():
    src = Path("src/clio_agent/gact/agents/runtime.py").read_text(encoding="utf-8")
    assert '("workflow_state", "evidence", "errors", "delegation")' in src
    assert '"artifacts"' not in src


def test_structured_field_specs_has_no_artifacts_entry():
    src = Path("src/clio_agent/gact/agents/builders.py").read_text(encoding="utf-8")
    # The injected structured spec dict declares ONLY workflow_state.
    m = re.search(
        r"_structured_field_specs:\s*dict\[str, tuple\[str, Any\]\]\s*=\s*\{(.+?)\}", src, re.S
    )
    assert m is not None
    assert "artifacts" not in m.group(1)
    assert "workflow_state" in m.group(1)


# --------------------------------------------------------------------------- #
# Finding [2]: inline-content write discipline (mode / overwrite / policy)
# --------------------------------------------------------------------------- #


def test_content_write_refused_would_overwrite_unregistered_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    app, sess, _ = _make_app(tmp_path)
    # Real user data already in the workspace — NOT a registered artifact.
    victim = tmp_path / "results.csv"
    victim.write_text("real,data\n1,2\n", encoding="utf-8")
    out = promote_proposal(
        app,
        sess.id,
        Proposal(name="results.csv", kind="report", content="junk"),
        workspace_id="ws1",
    )
    # Sabotage: drop the overwrite guard -> write clobbers results.csv, accepted -> red.
    assert out.accepted is False
    assert out.reason == RejectionReason.WOULD_OVERWRITE.value
    # The user's bytes are intact — nothing was written.
    assert victim.read_text(encoding="utf-8") == "real,data\n1,2\n"


def test_content_write_reversions_own_registered_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    app, sess, _ = _make_app(tmp_path)
    first = promote_proposal(
        app,
        sess.id,
        Proposal(name="report.md", kind="report", content="v1\n"),
        workspace_id="ws1",
    )
    assert first.accepted and first.created and first.version.version == 1
    # Re-versioning YOUR OWN registered artifact of the same (workspace,name) is a
    # legitimate overwrite — it is allowed and mints a NEW version.
    second = promote_proposal(
        app,
        sess.id,
        Proposal(name="report.md", kind="report", content="v2 changed\n"),
        workspace_id="ws1",
    )
    assert second.accepted and second.created
    assert second.version.version == 2
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == "v2 changed\n"


def test_content_write_refused_in_plan_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    app, sess, _ = _make_app(tmp_path, mode="plan")
    out = promote_proposal(
        app,
        sess.id,
        Proposal(name="report.md", kind="report", content="x"),
        workspace_id="ws1",
    )
    # P1.1 #1063: plan mode denies a content write through the UNIFIED resolver (the built-in
    # plan_acl @40 rule), surfaced as the typed POLICY_DENIED — no separate mode predicate.
    # Sabotage: drop the plan_acl deny -> write proceeds in read-only mode -> red.
    assert out.accepted is False
    assert out.reason == RejectionReason.POLICY_DENIED.value
    assert not (tmp_path / "report.md").exists()


def test_path_proposal_not_mode_gated_in_plan_mode(tmp_path, monkeypatch):
    # Registering an EXISTING file (path channel) stays NON-destructive: plan/
    # architect mode must NOT refuse it (only content writes are gated).
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    app, sess, _ = _make_app(tmp_path, mode="plan")
    f = tmp_path / "existing.md"
    f.write_text("already here", encoding="utf-8")
    out = promote_proposal(app, sess.id, Proposal(path=str(f), kind="report"), workspace_id="ws1")
    assert out.accepted and out.created


def test_content_write_policy_deny_gated_with_audit_row(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    app, sess, _ = _make_app(
        tmp_path,
        policies=[{"scope": "session", "tool_name_pattern": "create_artifact", "action": "deny"}],
    )
    out = promote_proposal(
        app,
        sess.id,
        Proposal(name="report.md", kind="report", content="x"),
        workspace_id="ws1",
    )
    assert out.accepted is False
    assert out.reason == RejectionReason.POLICY_DENIED.value
    assert not (tmp_path / "report.md").exists()
    # An audit row landed in /v1/permissions (auto_denied) — same as bridge writes.
    rows = list(app.state.permissions.values())
    assert any(
        r["action"] == "deny" and r["tool_call"]["tool_name"] == "create_artifact" for r in rows
    )


def test_content_write_policy_ask_is_gated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    app, sess, _ = _make_app(
        tmp_path,
        policies=[{"scope": "session", "tool_name_pattern": "create_artifact", "action": "ask"}],
    )
    out = promote_proposal(
        app,
        sess.id,
        Proposal(name="report.md", kind="report", content="x"),
        workspace_id="ws1",
    )
    # No inline approver on the native tool path -> ask is refused typed (fail-safe).
    assert out.accepted is False
    assert out.reason == RejectionReason.PERMISSION_REQUIRED.value
    assert not (tmp_path / "report.md").exists()


def test_content_write_policy_allow_records_audit_row(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    app, sess, _ = _make_app(
        tmp_path,
        policies=[{"scope": "session", "tool_name_pattern": "create_artifact", "action": "allow"}],
    )
    out = promote_proposal(
        app,
        sess.id,
        Proposal(name="report.md", kind="report", content="ok\n"),
        workspace_id="ws1",
    )
    assert out.accepted and out.created
    rows = list(app.state.permissions.values())
    assert any(
        r["action"] == "allow" and r["tool_call"]["tool_name"] == "create_artifact" for r in rows
    )


# --------------------------------------------------------------------------- #
# P4 plan-mode plan-file carve-out: the content-write gate must hand the RESOLVED
# target path to the resolver so the built-in plan_acl @70 ``<plans>/*.md`` carve-out
# can match. Without a path key in the consult args the @70 carve-out can NEVER win,
# so the model cannot write its designated plan file and plan_exit is unreachable.
# --------------------------------------------------------------------------- #


def test_content_write_in_plan_mode_allows_designated_plan_file(tmp_path, monkeypatch):
    """A plan-mode content write to ``<plans>/*.md`` is ALLOWED (the @70 carve-out matches).

    This is the headline defect: the gate previously consulted the resolver with
    ``{"name", "content_bytes"}`` and NO path key, so ``_permission_path_from_args``
    returned ``""`` and the @70 plan-file carve-out (keyed on ``path_pattern``) could
    never match — every create_artifact write in plan mode fell to the @40 deny.

    Sabotage: drop the ``"path"`` key from the consult args -> the carve-out can't
    match -> POLICY_DENIED -> red.
    """
    from clio_agent.gact.runtime import plan_acl  # noqa: PLC0415

    plans = tmp_path / ".clio" / "plans"
    monkeypatch.setattr(plan_acl, "plans_dir", lambda: plans.resolve())
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    app, sess, _ = _make_app(tmp_path, mode="plan")
    # The live target shape: <repo>/.clio/plans/<slug>-<sid>.md via create_artifact.
    out = promote_proposal(
        app,
        sess.id,
        Proposal(name=".clio/plans/my-plan.md", kind="report", content="# Plan\n"),
        workspace_id="ws1",
    )
    assert out.accepted and out.created
    assert (plans / "my-plan.md").read_text(encoding="utf-8") == "# Plan\n"
    # An audit row landed as auto_approved through the SAME resolver every write uses.
    rows = list(app.state.permissions.values())
    assert any(
        r["action"] == "allow" and r["tool_call"]["tool_name"] == "create_artifact" for r in rows
    )


def test_content_write_in_plan_mode_denies_non_plan_file(tmp_path, monkeypatch):
    """A plan-mode content write OUTSIDE the plans dir stays denied (the @40 deny holds).

    The carve-out is scoped to ``<plans>/*.md``; any other workspace path must still be
    refused typed POLICY_DENIED even now that the resolved path is provided.

    Sabotage: widen the carve-out to match every path -> this write is allowed -> red.
    """
    from clio_agent.gact.runtime import plan_acl  # noqa: PLC0415

    plans = tmp_path / ".clio" / "plans"
    monkeypatch.setattr(plan_acl, "plans_dir", lambda: plans.resolve())
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    app, sess, _ = _make_app(tmp_path, mode="plan")
    out = promote_proposal(
        app,
        sess.id,
        Proposal(name="report.md", kind="report", content="x"),
        workspace_id="ws1",
    )
    assert out.accepted is False
    assert out.reason == RejectionReason.POLICY_DENIED.value
    assert not (tmp_path / "report.md").exists()


def test_content_write_edit_mode_plan_path_unaffected(tmp_path, monkeypatch):
    """A content write in a NON-plan mode is unaffected by the plan_acl rows.

    Edit/chat mode has no plan_acl restriction, so a write anywhere in the workspace
    (including under the plans dir) proceeds regardless of the plans carve-out. Proves
    the fix did not change the non-plan write path.
    """
    from clio_agent.gact.runtime import plan_acl  # noqa: PLC0415

    plans = tmp_path / ".clio" / "plans"
    monkeypatch.setattr(plan_acl, "plans_dir", lambda: plans.resolve())
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    app, sess, _ = _make_app(tmp_path, mode="chat")
    out = promote_proposal(
        app,
        sess.id,
        Proposal(name="report.md", kind="report", content="ok\n"),
        workspace_id="ws1",
    )
    assert out.accepted and out.created
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == "ok\n"


def test_content_write_gate_records_resolved_target_path_in_audit_row(tmp_path, monkeypatch):
    """The consult+audit args carry the RESOLVED target path, not just name/content_bytes.

    Proves the path actually reaches the resolver (the audit row is built from the SAME
    ``args`` dict handed to ``_policy_action_for_tool``), so path-pattern policy rows can
    match. Sabotage: drop the ``"path"`` key from the args -> the audit input lacks the
    resolved path -> red.
    """
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    app, sess, _ = _make_app(
        tmp_path,
        policies=[{"scope": "session", "tool_name_pattern": "create_artifact", "action": "allow"}],
    )
    out = promote_proposal(
        app,
        sess.id,
        Proposal(name="report.md", kind="report", content="ok\n"),
        workspace_id="ws1",
    )
    assert out.accepted and out.created
    target = str((tmp_path / "report.md").resolve(strict=False))
    rows = [
        r
        for r in app.state.permissions.values()
        if r["tool_call"]["tool_name"] == "create_artifact"
    ]
    assert rows, "no create_artifact audit row recorded"
    inp = rows[0]["tool_call"]["input"]
    assert inp.get("path") == target
    # name / content_bytes are preserved alongside the new path key.
    assert inp.get("name") == "report.md"
    assert inp.get("content_bytes") == len(b"ok\n")


# --------------------------------------------------------------------------- #
# Finding [4/5/9]: path channel grounds relative paths against the workspace root
# --------------------------------------------------------------------------- #


def test_relative_path_registers_workspace_file_not_cwd(tmp_path, monkeypatch):
    # The workspace root differs from the process CWD (the normal deployed case).
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "analysis.md").write_text("workspace findings", encoding="utf-8")
    app, sess, _ = _make_app(tmp_path, workspace_root=ws)
    monkeypatch.chdir(tmp_path)  # cwd != workspace root
    out = promote_proposal(
        app, sess.id, Proposal(path="analysis.md", kind="report"), workspace_id="ws1"
    )
    # Sabotage: resolve proposal.path against CWD (drop the root-join) -> the
    # workspace file is not found -> path_missing -> red.
    assert out.accepted and out.created
    assert out.version.sha256 == hashlib.sha256((ws / "analysis.md").read_bytes()).hexdigest()


def test_relative_path_cannot_register_cwd_pollution(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    # A file that exists ONLY in the process CWD, not in the workspace root.
    (cwd / "leak.md").write_text("cwd pollution", encoding="utf-8")
    app, sess, _ = _make_app(tmp_path, workspace_root=ws)
    monkeypatch.chdir(cwd)
    out = promote_proposal(
        app, sess.id, Proposal(path="leak.md", kind="report"), workspace_id="ws1"
    )
    # Grounded against the workspace root, leak.md is absent there -> typed reject,
    # and the CWD file is never hashed/registered.
    assert out.accepted is False
    assert out.reason == RejectionReason.PATH_MISSING.value
    assert get_registry(app).get("ws1", "leak.md") is None


# --------------------------------------------------------------------------- #
# Finding [1]: batch length bound (one typed over_batch event, at the boundary)
# --------------------------------------------------------------------------- #


def test_batch_over_max_rejected_with_single_event(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "proposals_batch_max", lambda: 3)
    app, sess, arc = _make_app(tmp_path)
    # At the boundary: EXACTLY max items is allowed (each validated independently).
    ok_items = []
    for i in range(3):
        f = tmp_path / f"b{i}.md"
        f.write_text(f"b{i}", encoding="utf-8")
        ok_items.append(Proposal(path=str(f), kind="report"))
    res_ok = promote_proposals(app, sess.id, ok_items, workspace_id="ws1")
    assert res_ok["accepted"] == 3
    # One over the max -> the WHOLE batch is rejected with ONE typed over_batch event.
    before = len(_proposal_events(arc))
    over = [Proposal(kind="report") for _ in range(4)]
    res = promote_proposals(app, sess.id, over, workspace_id="ws1")
    # Sabotage: drop the batch bound -> each of the 4 items emits an event and
    # rejected==4 -> these assertions redden.
    assert res["accepted"] == 0
    assert res["rejected"] == 1
    assert len(res["artifacts"]) == 1
    assert res["artifacts"][0]["reason"] == RejectionReason.OVER_BATCH.value
    assert len(_proposal_events(arc)) - before == 1


# --------------------------------------------------------------------------- #
# Finding [3]: artifact.proposed designation vs file_diff shapes coexist
# --------------------------------------------------------------------------- #


def test_designation_and_file_diff_proposal_events_coexist(tmp_path):
    # Both producers share the type string 'artifact.proposed'; a consumer keys on
    # the additive 'stage' discriminator to tell them apart without mis-parse.
    app, sess, arc = _make_app(tmp_path)
    f = tmp_path / "a.md"
    f.write_text("x", encoding="utf-8")
    promote_proposal(app, sess.id, Proposal(path=str(f), kind="report"), workspace_id="ws1")
    designation = _proposal_events(arc)
    assert designation
    payload = designation[-1].payload
    # Sabotage: drop the 'stage' discriminator from the designation payload -> a
    # type-filtering consumer can't distinguish the two producers -> red.
    assert payload["stage"] == "designation"
    assert "designation" in payload
    assert "unified_diff" not in payload  # NOT the file_diff shape

    from clio_agent.gact.artifacts.wire import PROPOSED_ARTIFACT_EVENT, proposed_diff_payload

    # Same type string, disjoint (file_diff) shape — no 'stage', its own keys.
    assert PROPOSED_ARTIFACT_EVENT == P.PROPOSED_ARTIFACT_EVENT
    file_diff = proposed_diff_payload(
        path="n.py",
        unified_diff="@@",
        new_content="x\n",
        edit_mode="diff",
        lines_added=1,
        lines_removed=0,
    )
    assert file_diff.get("stage") is None
    assert "unified_diff" in file_diff


# --------------------------------------------------------------------------- #
# Finding [7]: created-flag honesty under a concurrent (racing) dedup
# --------------------------------------------------------------------------- #


def test_concurrent_dedup_reports_created_false_and_consumes_no_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "proposals_per_turn", lambda: 5)
    app, sess, _ = _make_app(tmp_path)
    f = tmp_path / "race.md"
    f.write_text("identical bytes", encoding="utf-8")
    # First designation mints v1 normally and consumes one cap slot.
    first = promote_proposal(
        app, sess.id, Proposal(path=str(f), kind="report"), workspace_id="ws1", turn_id="t1"
    )
    assert first.accepted and first.created
    assert P.proposal_count(app, sess.id, "t1") == 1

    # Simulate the race: the pre-mint dedup READ misses (a parallel promote had not
    # yet folded), so promote proceeds into registry.mint — whose authoritative
    # same-sha dedup fires. Force the first registry.get to miss, then restore.
    registry = get_registry(app)
    real_get = registry.get
    calls = {"n": 0}

    def racing_get(ws, name):
        calls["n"] += 1
        if calls["n"] == 1:  # the promote_proposal pre-check read
            return None
        return real_get(ws, name)

    monkeypatch.setattr(registry, "get", racing_get)
    second = promote_proposal(
        app, sess.id, Proposal(path=str(f), kind="report"), workspace_id="ws1", turn_id="t1"
    )
    # Sabotage: build ProposalOutcome(created=True) + increment cap unconditionally
    # (ignore mint.created) -> created/reason/cap assertions redden.
    assert second.accepted is True
    assert second.created is False
    assert second.reason == "already_registered"
    # No cap slot consumed on the concurrent dedup.
    assert P.proposal_count(app, sess.id, "t1") == 1
    # Still exactly one version in the chain.
    assert len(real_get("ws1", "race.md").versions) == 1
