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
