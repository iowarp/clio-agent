"""Scientific documents: a paper is one artifact built from tracked source files.

The decisions are in ``docs/design/latex-and-comments-ledger.md`` (the paper artifact rows):
the agent's source files are tracked as they are written, keyed by their workspace path; a
build makes a paper version (the PDF) that names those source versions as its members and
uses them in its lineage; and the reply shows the paper, not its members.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.gact.artifacts.minting import mint_tool_declared_outputs
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.turn_outputs import drain_turn_artifacts


class _Arc:
    def __init__(self) -> None:
        self.events: list = []

    def record_semantic_event(self, event):
        self.events.append(event)
        return event


class _Workspaces:
    def __init__(self, roots: dict) -> None:
        self._roots = roots

    def get(self, wid):
        root = self._roots.get(wid)
        return SimpleNamespace(root_path=root) if root else None


def _app(tmp_path: Path):
    from clio_agent.gact.sessions import SessionStore

    store = SessionStore(path=tmp_path / "sessions.json")
    session = store.create(workspace_id="ws1", title="t")
    state = SimpleNamespace(
        sessions=store,
        arc=_Arc(),
        workspaces=_Workspaces({"ws1": str(tmp_path)}),
        semantic_event_sink=object(),
        semantic_trace_detail_level="semantic",
        semantic_trace_backend=None,
        artifact_registry=None,
    )
    return SimpleNamespace(state=state), session


def _write(app, sid: str, path: Path, text: str, *, call: str, turn: str = "turn_1"):
    started = time.time() - 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return mint_tool_declared_outputs(
        app,
        sid,
        tool_name="fs_write_file",
        effective_args={"path": str(path), "content": text},
        call_id=call,
        workspace_id="ws1",
        turn_id=turn,
        call_started_at=started,
        result={"written_path": str(path)},
    )


def test_a_papers_source_files_are_tracked_by_their_workspace_path(tmp_path: Path) -> None:
    app, session = _app(tmp_path)
    [intro] = _write(app, session.id, tmp_path / "a/sections/intro.tex", "\\section{A}", call="c1")
    [other] = _write(app, session.id, tmp_path / "b/sections/intro.tex", "\\section{B}", call="c2")
    [bib] = _write(app, session.id, tmp_path / "a/refs.bib", "@misc{k,}", call="c3")
    registry = get_registry(app)
    names = {
        registry.get_by_artifact_id(version.artifact_id)[0].name for version in (intro, other, bib)
    }
    # Two papers' sections/intro.tex are two files, not two versions of one.
    assert names == {"a/sections/intro.tex", "b/sections/intro.tex", "a/refs.bib"}
    assert intro.artifact_id != other.artifact_id
    # Editing a source makes its next version.
    [again] = _write(app, session.id, tmp_path / "a/sections/intro.tex", "\\section{A2}", call="c4")
    assert again.version == 2


def test_other_outputs_keep_their_basename_chain(tmp_path: Path) -> None:
    app, session = _app(tmp_path)
    [chart] = _write(app, session.id, tmp_path / "out/chart.md", "# c", call="c1")
    assert get_registry(app).get_by_artifact_id(chart.artifact_id)[0].name == "chart.md"


def test_a_source_written_and_never_built_is_still_the_turns_output(tmp_path: Path) -> None:
    app, session = _app(tmp_path)
    [notes] = _write(app, session.id, tmp_path / "notes.tex", "\\section{N}", call="c1")
    buffered = drain_turn_artifacts(app, session.id, "turn_1")
    assert [entry["version"].artifact_id for entry in buffered] == [notes.artifact_id]


# --------------------------------------------------------------------------- #
# The paper: its name, title and labels
# --------------------------------------------------------------------------- #


def test_a_paper_is_named_after_its_project_folder(tmp_path: Path) -> None:
    from clio_agent.gact.documents.latex.paper_artifact import paper_name

    assert paper_name(tmp_path, tmp_path / "paper-bb/main.tex") == "paper-bb"
    assert paper_name(tmp_path, tmp_path / "paper-bb/supplement.tex") == "paper-bb/supplement"
    assert paper_name(tmp_path, tmp_path / "main.tex") == "main"


def test_the_title_reads_as_the_paper_shows_it() -> None:
    from clio_agent.gact.documents.latex.paper_artifact import paper_title

    entry = (
        "\\documentclass{article}\n% \\title{Not this one}\n"
        "\\title[Short]{Burst Buffers\\\\ on \\emph{Shared} Storage\\thanks{Funded by X.}}\n"
    )
    assert paper_title(entry) == "Burst Buffers on Shared Storage"
    assert paper_title("\\documentclass{article}\n") == ""


def test_the_type_and_venue_come_from_the_template_the_class_matches(tmp_path: Path) -> None:
    from clio_agent.gact.documents.latex.paper_artifact import document_labels

    for name, options, extra in (
        ("ieee-conference", "[conference]", "document_type: Paper\nvenue: IEEE conference\n"),
        ("ieee-journal", "[journal]", ""),
        ("nsf-proposal", "[]", "document_type: Proposal\nvenue: NSF\nclass: nsfprop\n"),
    ):
        folder = tmp_path / name
        folder.mkdir()
        cls = "" if "class:" in extra else "class: IEEEtran\n"
        (folder / "template.yaml").write_text(
            f"name: {name}\ntitle: {name} title\n{cls}options: {options}\n{extra}"
        )
    labels = document_labels("\\documentclass[conference,10pt]{IEEEtran}", [tmp_path])
    assert labels == {
        "document_type": "Paper",
        "venue": "IEEE conference",
        "template": "ieee-conference",
    }
    # A template without a venue line is named by its title; without a type, it is a Paper.
    journal = document_labels("\\documentclass[journal]{IEEEtran}", [tmp_path])
    assert journal == {
        "document_type": "Paper",
        "venue": "ieee-journal title",
        "template": "ieee-journal",
    }
    proposal = document_labels("\\documentclass{nsfprop}", [tmp_path])
    assert proposal["document_type"] == "Proposal" and proposal["venue"] == "NSF"
    assert document_labels("\\documentclass{article}", [tmp_path]) == {
        "document_type": "Paper",
        "venue": "",
        "template": "",
    }


# --------------------------------------------------------------------------- #
# A live build: the paper version, its members, lineage and comments
# --------------------------------------------------------------------------- #


def _tectonic_available() -> bool:
    import os

    from clio_agent.runtime.tectonic import installed_tectonic

    override = os.environ.get("CLIO_DOCUMENT_TECTONIC", "").strip()
    return bool(override and Path(override).is_file()) or installed_tectonic() is not None


@pytest.mark.usefixtures("host_agent_executor")
@pytest.mark.skipif(
    not _tectonic_available(), reason="no pinned Tectonic is installed (CLIO_DOCUMENT_TECTONIC)"
)
def test_a_build_makes_a_paper_built_from_its_member_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from clio_agent.gact.app import build_app
    from clio_agent.gact.documents.comment_sessions import mark_target
    from clio_agent.gact.documents.review_render import _header

    monkeypatch.setenv("CLIO_SANDBOX_ENABLED", "false")  # the build, not the fence (L22)
    root = tmp_path / "ws"
    project = root / "paper-bb"
    (project / "sections").mkdir(parents=True)
    (project / "main.tex").write_text(
        "\\documentclass{article}\n\\title{Burst Buffers\\thanks{Funded.}}\n"
        "\\begin{document}\n\\maketitle\n\\input{sections/intro}\n\\end{document}\n"
    )
    intro = project / "sections" / "intro.tex"
    intro.write_text("\\section{Introduction}\nCheckpoints are bursts.\n")
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        workspace_id = client.post(
            "/v1/workspaces", json={"name": "paper", "root_path": str(root)}
        ).json()["id"]
        session_id = client.post("/v1/sessions", json={"workspace_id": workspace_id}).json()["id"]
        entry_id = client.post(
            f"/v1/workspaces/{workspace_id}/comment-targets",
            json={"path": "paper-bb/main.tex", "session_id": session_id},
        ).json()["artifact"]["artifact_id"]

        def report(artifact_id: str) -> dict:
            response = client.get(
                f"/v1/artifacts/{artifact_id}/latex", params={"session_id": session_id}
            )
            assert response.status_code == 200, response.text
            return response.json()

        first = report(entry_id)["build"]["paper"]
        assert first["name"] == "paper-bb" and first["title"] == "Burst Buffers"
        assert first["document_type"] == "Paper" and first["version"] == 1
        assert first["members"] == ["main.tex", "sections/intro.tex"] and not first["untracked"]
        paper = client.get(f"/v1/artifacts/{first['artifact_id']}").json()
        producer = paper["resolved"]["producer"]
        assert producer["designation"] == "scientific-document"
        assert producer["entry"] == "paper-bb/main.tex"
        assert {item["name"] for item in producer["members"]} == {
            "paper-bb/main.tex",
            "paper-bb/sections/intro.tex",
        }

        # The lineage: each member version was used by the build that generated the paper.
        lineage = client.get(
            f"/v1/artifacts/{first['artifact_id']}/lineage",
            params={"direction": "upstream", "depth": 2},
        ).json()
        used = {edge["from"] for edge in lineage["edges"] if edge["type"] == "used"}
        assert {item["artifact_id"] for item in producer["members"]} <= used

        # The paper's own id opens the workspace; an unchanged rebuild is the same version.
        assert report(first["artifact_id"])["build"]["paper"]["version"] == 1
        # A section-only edit makes the paper's next version.
        intro.write_text("\\section{Introduction}\nCheckpoints are large bursts.\n")
        second = report(first["artifact_id"])["build"]["paper"]
        assert second["name"] == "paper-bb" and second["version"] == 2
        assert second["artifact_id"] != first["artifact_id"]

        # A comment on the paper resolves against its entry, on that paper version's PDF.
        app = client.app
        from clio_agent.gact.artifacts.registry import get_registry

        record, version = get_registry(app).get_by_artifact_id(first["artifact_id"])
        target = mark_target(app, record, version, session_id)
        assert target.record.name == "paper-bb/main.tex"
        assert target.shown is not None and target.shown[1].artifact_id == first["artifact_id"]
        assert target.view_path != target.path
        header = " ".join(_header(target))
        assert 'the paper "Burst Buffers" (paper-bb;' in header and "2 source file(s)" in header
