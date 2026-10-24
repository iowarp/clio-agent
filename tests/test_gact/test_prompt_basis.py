"""How a plugin change reaches the model: now, after a compaction, or as a note.

The hermetic test marketplace's ``clio/standard`` carries core, workspace, shell,
visuals, delegation and document-runtime (which requires shell).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agents import prompt_basis
from clio_agent.gact.app import build_app
from tests._harness import runner_module_builder


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seen: list[dict[str, Any]]) -> Any:
    from clio_agent.gact import context
    from clio_agent.gact.agents import builders

    def runner(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        del base_agent, session_id
        seen.append(
            {
                "question": question,
                "tools": list(agent_def.tools),
                "metadata": dict(agent_def.metadata),
                "injections": dict(context.turn_injections()),
            }
        )
        return SimpleNamespace(answer=f"ok {len(seen)}")

    monkeypatch.setattr(builders, "build_agent_module", runner_module_builder(runner))
    return build_app(sessions_path=tmp_path / "sessions.json", agent=object())


def _patch(client: TestClient, sid: str, **body: Any) -> dict[str, Any]:
    resp = client.patch(f"/v1/sessions/{sid}/agent", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_a_note_keeps_the_prompt_and_tells_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .conftest import complete_turn

    seen: list[dict[str, Any]] = []
    with TestClient(_app(tmp_path, monkeypatch, seen)) as client:
        sid = client.post("/v1/sessions", json={"title": "note"}).json()["id"]
        before = client.get(f"/v1/sessions/{sid}/agent").json()
        assert before["prompt_basis"]["pending"] is False

        body = _patch(client, sid, remove=["shell"], apply="note")
        basis = body["prompt_basis"]
        assert basis["digest"] == before["resolution"]["digest"] != body["resolution"]["digest"]
        assert basis["pending"] is True
        assert basis["removed"] == ["shell", "document-runtime"]
        assert "shell_bash" in basis["tools_still_listed"]

        complete_turn(client, sid, "after the change")
        # The prompt (and its tool list) is still the one the model has cached...
        assert "shell_bash" in seen[-1]["tools"]
        assert seen[-1]["metadata"]["prompt_basis"] == basis["digest"]
        # ...and the change reaches it as a note.
        note = seen[-1]["injections"][prompt_basis.PLUGIN_CHANGE_SOURCE]
        assert "Removed: shell, document-runtime" in note
        assert "next compaction" in note


def test_an_added_plugin_is_introduced_and_runs_but_stays_unlisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .conftest import complete_turn

    seen: list[dict[str, Any]] = []
    with TestClient(_app(tmp_path, monkeypatch, seen)) as client:
        sid = client.post("/v1/sessions", json={"title": "add"}).json()["id"]
        _patch(client, sid, remove=["shell"], apply="now")
        body = _patch(client, sid, remove=[], apply="note")
        assert body["prompt_basis"]["added"] == ["shell", "document-runtime"]
        assert "shell_bash" in body["prompt_basis"]["tools_unlisted"]

        complete_turn(client, sid, "use the shell")
        metadata = seen[-1]["metadata"]
        # Built and callable, but left out of the request's tool list.
        assert "shell_bash" in seen[-1]["tools"]
        assert "shell_bash" in metadata["unlisted_tools"]
        assert "Added: shell." in seen[-1]["injections"][prompt_basis.PLUGIN_CHANGE_SOURCE]


def test_apply_now_clears_a_held_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from .conftest import complete_turn

    seen: list[dict[str, Any]] = []
    with TestClient(_app(tmp_path, monkeypatch, seen)) as client:
        sid = client.post("/v1/sessions", json={"title": "now"}).json()["id"]
        _patch(client, sid, remove=["shell"], apply="note")
        body = _patch(client, sid, remove=["shell"], apply="now")
        assert body["prompt_basis"]["pending"] is False
        assert body["prompt_basis"]["digest"] == body["resolution"]["digest"]

        complete_turn(client, sid, "plain")
        assert "shell_bash" not in seen[-1]["tools"]
        assert prompt_basis.PLUGIN_CHANGE_SOURCE not in seen[-1]["injections"]


def test_compact_with_nothing_to_fold_applies_the_change_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    with TestClient(_app(tmp_path, monkeypatch, seen)) as client:
        sid = client.post("/v1/sessions", json={"title": "compact"}).json()["id"]
        body = _patch(client, sid, remove=["shell"], apply="compact")
        # A fresh session has no live context: nothing to fold, so the change applies.
        assert body["compaction"]["compacted"] is False
        assert body["compaction"]["reason"] == "no_live_context"
        assert body["prompt_basis"]["pending"] is False
        assert body["prompt_basis"]["digest"] == body["resolution"]["digest"]


def test_a_completed_compaction_settles_a_held_note(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact import compaction

    settled: list[str] = []
    monkeypatch.setattr(prompt_basis, "settle_basis", lambda app, sid: settled.append(sid))
    sessions = SimpleNamespace(get=lambda sid: object())
    app = SimpleNamespace(state=SimpleNamespace(sessions=sessions, arc=object()))
    monkeypatch.setattr(compaction.history_mode, "active", lambda: False)
    monkeypatch.setattr(compaction, "_open_turn_id", lambda app, sid: "")
    monkeypatch.setattr(compaction, "_scopes", lambda app, sid, scope: ["main"])
    app.state.arc = SimpleNamespace(render_working_set=lambda sid, scope: ["segment"])
    monkeypatch.setattr(compaction, "_plan", lambda app, sid, scope, turn: object())
    monkeypatch.setattr(compaction, "_compact_scope", lambda *args: {"compaction_id": "c1"})

    result = compaction.compact_session_context(app, "sess_1", trigger="auto")

    assert result["compacted"] is True and settled == ["sess_1"]


def test_apply_mode_is_validated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(_app(tmp_path, monkeypatch, [])) as client:
        sid = client.post("/v1/sessions", json={"title": "bad"}).json()["id"]
        resp = client.patch(f"/v1/sessions/{sid}/agent", json={"remove": [], "apply": "later"})
        assert resp.status_code == 422, resp.text


def test_unlisted_names_cover_added_tools_and_added_servers() -> None:
    metadata = {"unlisted_tools": ["notes_add"], "unlisted_servers": ["hdf5"]}
    names = ["notes_add", "hdf5_read", "shell_bash", "hdf5x_other"]
    assert prompt_basis.unlisted_tool_names(metadata, names) == {"notes_add", "hdf5_read"}
    assert prompt_basis.unlisted_tool_names({}, names) == frozenset()


def test_tool_introductions_join_the_change_note() -> None:
    tool = SimpleNamespace(
        name="hdf5_read",
        args={"path": {"type": "string"}, "dataset": {"type": "string"}},
        desc="Read one dataset from an HDF5 file.",
    )
    injections = [("todos", "t"), (prompt_basis.PLUGIN_CHANGE_SOURCE, "Added: hdf5.")]
    out = dict(prompt_basis.with_tool_introductions(injections, [tool]))
    assert out["todos"] == "t"
    note = out[prompt_basis.PLUGIN_CHANGE_SOURCE]
    assert note.startswith("Added: hdf5.\nNew tools you can call now")
    assert "- hdf5_read(path: string, dataset: string): Read one dataset from an HDF5 file." in note


def test_the_note_is_folded_into_the_user_turn_as_tagged_text() -> None:
    """The default rendering every chat template accepts: a mid-conversation system or
    developer message is refused by strict templates (Qwen3 on vLLM / llama.cpp), so
    native operator messages are a per-provider capability, not the default."""

    from clio_agent.gact.agents.clio_react_record import user_from_record

    note = user_from_record(
        {"text": "Removed: shell.", "actor": "algorithm", "source": "plugin_change"}
    )
    assert note.role == "user"
    assert note.parts[0].text == "[clio: plugin_change]\nRemoved: shell."
