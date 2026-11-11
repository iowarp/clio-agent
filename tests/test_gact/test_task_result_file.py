"""#1844: a subagent result that cannot fit its parent's context is saved to a file.

There is no fixed size: the parent's room (its context window less what it carries)
decides (:mod:`clio_agent.gact.child_report_fit`). The parent gets the file's path and size and the result's beginning; the file holds
the exact result; the session owns it; old cursors still read their pages.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact import child_report_fit
from clio_agent.gact.agents.native_presenters import native_presentation
from clio_agent.gact.session_tool_output import delete_session_tool_output
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_result_content import result_cursor
from clio_agent.gact.task_result_spill import (
    RESULT_EXCERPT_CHARS,
    TASK_RESULT_SAVED_REASON,
    saved_result_block,
)
from tests.test_gact.test_flow_tool_evidence import _tool
from tests.test_gact.test_permission_evidence import world as world

pytestmark = pytest.mark.usefixtures("host_agent_executor", "tight_room")

#: About 200 KB of JSON once encoded, far past the room the fixture leaves.
BIG = {
    "blob": "é🚀x" * 70_000,
    "workflow_state": {"delivery": {"path": "D:/out/report.md", "status": "verified"}},
}


@pytest.fixture
def tight_room(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parent with room for about 10k tokens of reports."""
    monkeypatch.setattr(child_report_fit, "_measure", lambda: child_report_fit._Room(10_000))


def _read(client: Any, sid: str, handle: str, **params: Any) -> dict[str, Any]:
    response = client.get(f"/v1/sessions/{sid}/tasks/{handle}/result", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_forced_large_result_is_saved_whole_to_a_file(
    request: pytest.FixtureRequest,
) -> None:
    app, client, sid, root = request.getfixturevalue("world")
    task = _tool(app, root, BIG)

    page = _read(client, sid, task.handle)

    saved = page["result_file"]
    assert page["result"] is None and page["next_cursor"] is None
    assert saved["reason"] == TASK_RESULT_SAVED_REASON
    assert saved["media_type"] == "application/json"
    path = Path(saved["path"])
    assert path.is_file() and path.suffix == ".json"
    assert json.loads(path.read_text(encoding="utf-8")) == BIG
    assert saved["bytes"] == path.stat().st_size
    encoded = json.dumps(BIG, ensure_ascii=False, sort_keys=True)
    assert saved["chars"] == len(encoded)
    assert page["result_excerpt"] == encoded[:RESULT_EXCERPT_CHARS]
    assert page["result_keys"] == ["blob", "workflow_state"]
    assert sid in path.parts  # the reading session owns the file
    # Reading again reuses the same file.
    assert _read(client, sid, task.handle)["result_file"]["path"] == str(path)


def test_a_selected_string_is_saved_as_text(request: pytest.FixtureRequest) -> None:
    app, client, sid, root = request.getfixturevalue("world")
    task = _tool(app, root, BIG["blob"])

    page = _read(client, sid, task.handle)

    path = Path(page["result_file"]["path"])
    assert path.suffix == ".txt" and page["result_file"]["media_type"] == "text/plain"
    assert path.read_text(encoding="utf-8") == BIG["blob"]


def test_a_small_selection_of_a_large_result_comes_back_inline(
    request: pytest.FixtureRequest,
) -> None:
    app, client, sid, root = request.getfixturevalue("world")
    task = _tool(app, root, BIG)

    page = _read(client, sid, task.handle, select="/workflow_state")

    assert page["result"] == {"/workflow_state": BIG["workflow_state"]}
    assert "result_file" not in page and page["next_cursor"] is None


def test_a_result_that_fits_the_parents_room_comes_back_whole(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No fixed size: with room for it, a 200 KB result is returned whole.
    SABOTAGE: restore a fixed character bound in ``result_page`` and this fails."""
    monkeypatch.setattr(child_report_fit, "_measure", lambda: child_report_fit._Room(1_000_000))
    app, client, sid, root = request.getfixturevalue("world")
    task = _tool(app, root, BIG)

    page = _read(client, sid, task.handle)

    assert page["result"] == BIG and "result_file" not in page


def test_a_long_report_is_saved_as_readable_text(request: pytest.FixtureRequest) -> None:
    """A child's multi-line report is written as it reads, beside the exact JSON, and the
    parent is pointed at it. SABOTAGE: drop ``_text_fields`` and this fails."""
    app, client, sid, root = request.getfixturevalue("world")
    report = "\n".join(f"- `module_{i}.py:{i}` does thing {i}" for i in range(4_000))
    task = _tool(app, root, {"answer": report, "status": "done"})

    saved = _read(client, sid, task.handle)["result_file"]

    text = Path(saved["text_files"]["answer"])
    assert text.suffix == ".md" and text.read_text(encoding="utf-8") == report
    assert saved["read_with"] == f'fs_read_file(path="{text}", offset=1)'
    assert json.loads(Path(saved["path"]).read_text(encoding="utf-8"))["answer"] == report


def test_an_old_cursor_still_reads_its_pages(request: pytest.FixtureRequest) -> None:
    """Version-1 cursors from earlier releases sit in committed history."""
    app, client, sid, root = request.getfixturevalue("world")
    task = _tool(app, root, BIG)
    caller = TaskCaller(session_id=sid, human_id=root.owner.human_id)
    cursor: str | None = result_cursor(caller.session_id, task.handle, task.result, "task_result")
    pieces: list[str] = []
    while cursor is not None:
        page = _read(client, sid, task.handle, cursor=cursor)
        assert "result_file" not in page
        pieces.append(page["result"]["preview"])
        cursor = page["next_cursor"]

    assert len(pieces) > 1
    assert json.loads("".join(pieces)) == BIG


def test_deleting_the_session_deletes_its_saved_results(
    request: pytest.FixtureRequest,
) -> None:
    app, client, sid, root = request.getfixturevalue("world")
    task = _tool(app, root, BIG)
    path = Path(_read(client, sid, task.handle)["result_file"]["path"])
    assert path.is_file()

    # The session-delete route runs this cleanup (the fixture's session stays busy).
    workspace_id = app.state.sessions.get(sid).workspace_id
    assert delete_session_tool_output(app, workspace_id, sid) >= 1

    assert not path.exists()


def test_the_user_sees_an_open_full_result_link(request: pytest.FixtureRequest) -> None:
    app, client, sid, root = request.getfixturevalue("world")
    task = _tool(app, root, BIG)
    page = _read(client, sid, task.handle)

    presented = native_presentation("task_output", {"handle": task.handle}, page, page)

    blocks = [block for block in presented["blocks"] if block.get("type") == "link"]
    link = next(block for block in blocks if block["id"].endswith("saved-result"))
    assert link["target"] == "file" and link["uri"] == page["result_file"]["path"]
    assert link["label"].startswith("Full result saved (")
    assert link["action_label"] == "Open full result"
    assert link["text"] == page["result_excerpt"]
    assert "Full result is available through get_task_result." not in json.dumps(presented)
    assert presented["summary"].startswith("1 result collected")


def test_a_failed_save_says_so_without_a_link() -> None:
    block = saved_result_block(
        {
            "result_file": {"path": "", "bytes": 10, "reason": "task_result_save_failed"},
            "result_excerpt": "head",
        },
        0,
    )
    assert block is not None
    assert block["label"] == "Full result could not be saved (1 KB)"
    assert block["uri"] == "" and block["action_label"] == ""
