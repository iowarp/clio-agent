"""An uncertain call whose owner settled no longer holds its ancestors open."""

from __future__ import annotations

from types import SimpleNamespace

from clio_agent.gact.assignment_pending import orphaned_uncertainty, pending_work


def _task(handle: str, kind: str, status: str, owner: str, terminal: str | None = None):
    return SimpleNamespace(
        handle=handle,
        kind=kind,
        status=status,
        terminal_at=terminal,
        owner=SimpleNamespace(assignment_id=owner),
    )


def _history(rows: dict[str, SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(task_snapshot=rows.get, tree_handles=lambda _sid, active_only: set(rows))


def test_an_uncertain_call_of_a_finished_researcher_is_not_pending() -> None:
    rows = {
        "flow": _task("flow", "Subagent", "active", "flow"),
        "researcher": _task("researcher", "Subagent", "completed", "researcher", "t1"),
        "search": _task("search", "MCP", "task_state_uncertain", "researcher"),
    }
    history = _history(rows)
    assert orphaned_uncertainty(history, rows["search"])
    assert [row.handle for row in pending_work(history, "s", "flow")] == ["researcher"]


def test_an_uncertain_call_of_a_live_owner_still_counts() -> None:
    rows = {
        "researcher": _task("researcher", "Subagent", "active", "researcher"),
        "search": _task("search", "MCP", "task_state_uncertain", "researcher"),
    }
    assert not orphaned_uncertainty(_history(rows), rows["search"])


def test_running_work_and_uncertain_assignments_always_count() -> None:
    rows = {
        "done": _task("done", "Subagent", "completed", "done", "t1"),
        "shell": _task("shell", "Shell", "running", "done"),
        "child": _task("child", "Subagent", "task_state_uncertain", "done"),
    }
    history = _history(rows)
    assert not orphaned_uncertainty(history, rows["shell"])
    assert not orphaned_uncertainty(history, rows["child"])


def test_its_own_uncertain_call_does_not_hold_a_finishing_assignment_open() -> None:
    """The researcher answered after the restart without an explicit disposition.

    SABOTAGE: drop the ``settling`` branch and its own uncertain search keeps it
    ``yielded`` forever (the live crash acceptance of a deep-research run).
    """
    rows = {
        "researcher": _task("researcher", "Subagent", "active", "flow"),
        "search": _task("search", "MCP", "task_state_uncertain", "researcher"),
        "fetch": _task("fetch", "MCP", "running", "researcher"),
        "child": _task("child", "Subagent", "task_state_uncertain", "researcher"),
    }
    history = _history(rows)
    pending = pending_work(history, "s", "researcher")
    assert sorted(row.handle for row in pending) == ["child", "fetch"]
    # For an ancestor the live researcher is the pending work; its call still counts too.
    assert "search" in {row.handle for row in pending_work(history, "s", "flow")}
