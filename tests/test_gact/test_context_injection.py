"""iowarp/clio-agent#5: attached context files reach the agent.

Drives a fake agent that records the question text it receives;
asserts that attached context_files (mode=read) appear inline in
the prompt, and that mode=edit + paths-outside-root are filtered.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")


class _RecordingAgent:
    """Captures every question forwarded to it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def forward(self, question: str, session_id: str = "default"):
        self.calls.append((question, session_id))
        return type(
            "Pred",
            (),
            {
                "answer": "ok",
                "selected_expert": "",
                "routing_rationale": "",
            },
        )()


@pytest.fixture()
def setup(tmp_path: Path) -> Iterator[tuple[object, TestClient, _RecordingAgent, Path]]:
    """App + an ENTERED TestClient (one app-lifetime portal) + the fake agent.

    The client is entered for the whole test on purpose. A TestClient that is
    never entered gives EACH request its own anyio portal; ``POST /messages``
    returns an ack while the turn is still running, so the turn task — created
    on that transient portal's loop — is cancelled the instant the portal tears
    down. ``complete_turn`` then settles on a truthfully ``cancelled`` assistant
    message whose agent was never invoked, and ``agent.calls[-1]`` raises
    IndexError. Under coverage the turn is slow enough for the portal to win,
    which is how this module went red on CI.
    """

    agent = _RecordingAgent()
    # Pin the workspace root to tmp_path so context files live
    # under the policy boundary.
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    # Update ws_default's root_path so its files pass the
    # workspace check inside _enrich_with_context_files.
    app.state.workspaces.update("ws_default", root_path=str(tmp_path))
    with TestClient(app) as client:
        yield app, client, agent, tmp_path


def test_no_files_attached_passes_text_unchanged(setup) -> None:
    from .conftest import complete_turn

    app, c, agent, _ = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    complete_turn(c, sid, "hello")
    assert agent.calls == [("hello", sid)]


def test_read_mode_inlines_file_content(setup) -> None:
    from .conftest import complete_turn

    app, c, agent, tmp_path = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    fpath = tmp_path / "notes.md"
    fpath.write_text("# Project notes\nimportant insight\n")
    c.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": str(fpath), "mode": "read"},
    )
    assistant = complete_turn(c, sid, "summarise")
    seen, _ = agent.calls[-1]
    assert "Attached files" in seen
    assert "Project notes" in seen
    assert "important insight" in seen
    # Original prompt still present.
    assert "summarise" in seen
    context_files = assistant["metadata"]["context_files"]
    assert context_files["status"] == "prepared"
    assert context_files["count"] == 1
    assert context_files["max_inline_bytes"] == 32 * 1024
    assert context_files["files"][0]["path"] == str(fpath)
    assert context_files["files"][0]["mode"] == "read"
    assert context_files["files"][0]["inline_policy"] == "inline_or_inspect"
    assert context_files["files"][0]["status"] == "prepared"


def test_workspace_relative_mention_inlines_file_and_strips_at_marker(setup) -> None:
    from .conftest import complete_turn

    app, c, agent, tmp_path = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    fpath = tmp_path / "docs" / "notes.md"
    fpath.parent.mkdir()
    fpath.write_text("picker-selected context\n")
    c.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": "@docs/notes.md", "mode": "read"},
    )

    complete_turn(c, sid, "summarise @docs/notes.md")

    seen, _ = agent.calls[-1]
    assert "Attached files" in seen
    assert "### Context file: docs/notes.md" in seen
    assert "picker-selected context" in seen
    assert "@docs/notes.md" not in seen
    assert "summarise docs/notes.md" in seen


def test_edit_mode_includes_only_header(setup) -> None:
    from .conftest import complete_turn

    app, c, agent, tmp_path = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    fpath = tmp_path / "code.py"
    fpath.write_text("def f(): pass\n")
    c.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": str(fpath), "mode": "edit"},
    )
    assistant = complete_turn(c, sid, "rename")
    seen, _ = agent.calls[-1]
    assert "code.py" in seen
    assert "mode=edit" in seen
    # Body should NOT be inlined for edit mode.
    assert "def f()" not in seen
    assert assistant["metadata"]["context_files"]["files"][0]["inline_policy"] == ("metadata_only")


def test_missing_edit_mode_path_remains_visible(setup) -> None:
    from .conftest import complete_turn

    app, c, agent, tmp_path = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    fpath = tmp_path / "new_file.py"
    c.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": str(fpath), "mode": "edit"},
    )
    complete_turn(c, sid, "create it")
    seen, _ = agent.calls[-1]
    assert str(fpath) in seen
    assert "mode=edit" in seen
    assert "target does not exist yet" in seen


def test_read_file_deleted_after_attach_surfaces_error(setup) -> None:
    from .conftest import complete_turn

    app, c, agent, tmp_path = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    fpath = tmp_path / "notes.md"
    fpath.write_text("important context\n")
    c.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": str(fpath), "mode": "read"},
    )
    fpath.unlink()

    assistant = complete_turn(c, sid, "summarise")

    assert agent.calls == []
    assert assistant["stop_reason"] == "error"
    assert assistant["error_info"]["error"] == "context_file_error"
    assert assistant["error_info"]["details"]["path"] == str(fpath)
    assert assistant["error_info"]["details"]["operation"] == "exists"
    assert assistant["error_info"]["details"]["recovery_actions"] == [
        "reattach_context_file",
        "remove_context_file",
        "retry",
        "exit",
    ]
    assert assistant["metadata"]["context_files"]["status"] == "error"
    assert assistant["metadata"]["context_files"]["files"][0]["path"] == str(fpath)
    assert assistant["metadata"]["context_files"]["files"][0]["status"] == "error"
    completed = [ev for ev in app.state.bus._history.get(sid, []) if ev.type == "message.completed"]
    assert completed, "turn did not publish message.completed"
    payload = completed[-1].payload
    assert payload["message_id"] == assistant["id"]
    assert payload["stop_reason"] == "error"
    assert payload["error_info"]["error"] == "context_file_error"


def test_path_outside_workspace_root_is_inlined_for_reads(setup, tmp_path: Path) -> None:
    """iowarp/clio-agent#5 (revised): the workspace-root check used to
    silently skip context_files outside the root, but the user
    explicitly attaches via the API — they know what they're doing.
    Reads are trusted; writes still gated by _apply_edit_to_disk +
    plan_mode. Verify the body of an outside-root file IS visible."""

    from .conftest import complete_turn

    app, c, agent, _ = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    # Create the outside-root file in a separate OS-absolute temp dir so it
    # is absolute on every OS yet still outside the tmp_path workspace root.
    outside_dir = Path(tempfile.mkdtemp())
    outside = outside_dir / "v031-outside-marker.txt"
    outside.write_text("MAGIC-MARKER-FROM-OUTSIDE-ROOT")
    try:
        c.post(
            f"/v1/sessions/{sid}/context/files",
            json={"path": str(outside), "mode": "read"},
        )
        complete_turn(c, sid, "hi")
    finally:
        try:
            outside.unlink()
        except FileNotFoundError:
            pass
        try:
            outside_dir.rmdir()
        except OSError:
            pass
    seen, _ = agent.calls[-1]
    # File body IS inlined now (no silent skip).
    assert "MAGIC-MARKER-FROM-OUTSIDE-ROOT" in seen


def test_large_file_is_truncated(setup) -> None:
    from .conftest import complete_turn

    app, c, agent, tmp_path = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    fpath = tmp_path / "big.txt"
    fpath.write_text("x" * (40 * 1024))  # > 32 KB cap
    c.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": str(fpath), "mode": "read"},
    )
    complete_turn(c, sid, "what's in big.txt")
    seen, _ = agent.calls[-1]
    assert "more bytes truncated" in seen
