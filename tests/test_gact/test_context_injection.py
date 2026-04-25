"""iowarp/clio-agent#5: attached context files reach the agent.

Drives a fake agent that records the question text it receives;
asserts that attached context_files (mode=read) appear inline in
the prompt, and that mode=edit + paths-outside-root are filtered.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


class _RecordingAgent:
    """Captures every question forwarded to it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def forward(self, question: str, session_id: str = "default"):
        self.calls.append((question, session_id))
        return type("Pred", (), {
            "answer": "ok", "selected_expert": "",
            "routing_rationale": "",
        })()


@pytest.fixture()
def setup(tmp_path: Path):
    agent = _RecordingAgent()
    # Pin the workspace root to tmp_path so context files live
    # under the policy boundary.
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    # Update ws_default's root_path so its files pass the
    # workspace check inside _enrich_with_context_files.
    app.state.workspaces.update(
        "ws_default", root_path=str(tmp_path)
    )
    return app, TestClient(app), agent, tmp_path


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
    complete_turn(c, sid, "summarise")
    seen, _ = agent.calls[-1]
    assert "Attached files" in seen
    assert "Project notes" in seen
    assert "important insight" in seen
    # Original prompt still present.
    assert "summarise" in seen


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
    complete_turn(c, sid, "rename")
    seen, _ = agent.calls[-1]
    assert "code.py" in seen
    assert "mode=edit" in seen
    # Body should NOT be inlined for edit mode.
    assert "def f()" not in seen


def test_path_outside_workspace_root_is_skipped(
    setup, tmp_path: Path
) -> None:
    from .conftest import complete_turn

    app, c, agent, _ = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    # File under /tmp is outside the workspace root (also tmp_path
    # but a different subtree — TempPathFactory).
    outside = Path("/tmp") / "definitely-outside.txt"
    outside.write_text("secret")
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
    seen, _ = agent.calls[-1]
    # Either no Context section at all (file rejected) or no body inlined.
    assert "secret" not in seen


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
