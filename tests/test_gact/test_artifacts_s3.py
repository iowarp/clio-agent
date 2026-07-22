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


def _make_app(tmp_path: Path, *, workspace_root: Path | None = None):
    store = SessionStore(path=tmp_path / "sessions.json")
    sess = store.create(workspace_id="ws1", title="t")
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
    assert ver.custody == Custody.WORKSPACE_REFERENCED
    assert ver.kind == ArtifactKind.REPORT
    # HARNESS hash, not the model's claim.
    assert ver.sha256 == hashlib.sha256(report.read_bytes()).hexdigest()
    # Model intent is quarantined in annotation, never merged into evidence.
    assert ver.annotation == "the deliverable"
    # Registered in the projection.
    rec = get_registry(app).get("ws1", "analysis.md")
    assert rec is not None and rec.head is ver


def test_model_supplied_hash_is_ignored_harness_wins(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    report = tmp_path / "r.md"
    report.write_text("real bytes", encoding="utf-8")
    real = hashlib.sha256(report.read_bytes()).hexdigest()
    fake = "0" * 64
    # A model that puts a sha256 in the args — it MUST be ignored.
    proposal = Proposal.from_mapping(
        {"path": str(report), "kind": "report", "sha256": fake, "hash": fake}
    )
    # Sabotage: read proposal.sha into evidence -> assertion flips to `fake`.
    out = promote_proposal(app, sess.id, proposal, workspace_id="ws1")
    assert out.version is not None
    assert out.version.sha256 == real
    assert out.version.sha256 != fake


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


def test_react_branches_attach_create_artifact_unconditionally():
    """Both react builder branches attach create_artifact — NOT skill-gated (#969)."""
    src = Path("src/clio_agent/gact/agents/builders.py").read_text(encoding="utf-8")
    # Exactly the two react sites (blueprint experts + tool-user agents).
    calls = re.findall(r"build_create_artifact_tool\(agent_def\)", src)
    assert len(calls) == 2, f"expected 2 react-site attachments, found {len(calls)}"
    # It must NOT be nested only under an `if skill_rt.resolved:` guard: the
    # attach lines are dedented to the branch body, not the skill-gated block.
    assert "if skill_rt.resolved:\n                    # Auto-attached" in src


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
