"""Execution progress never substitutes a child's return for report acceptance."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from clio_agent.flows.execution_view import execution_view


def test_rejected_report_preserves_returned_children_and_unrun_delivery() -> None:
    content: dict[str, Any] = {
        "flow": "deep-research",
        "status": "flow_stall",
        "steps": {"p": {"name": "DeepResearch._plan"}, "w": {"name": "DeepResearch._write"}},
        "events": {
            "r": {
                "event": "report_review",
                "status": "issues_found",
                "issues": [{"reason": "Missing comparison"}],
            }
        },
        "result": {"step": "report", "message": "Review rejected"},
    }
    rows = execution_view(
        content,
        [
            {
                "invocation": "a",
                "target": "researcher",
                "status": "completed",
                "together": "a",
                "handle": "ta",
            },
            {
                "invocation": "b",
                "target": "researcher",
                "status": "completed",
                "together": "a",
                "handle": "tb",
            },
            {"invocation": "c", "target": "critic", "status": "completed", "handle": "tc"},
        ],
    )
    by_id = {row["id"]: row for row in rows}
    assert by_id["a"]["status"] == "returned"
    assert by_id["a"]["after"] == by_id["b"]["after"] == ["run:plan"]
    assert by_id["c"]["after"] == ["a", "b"]
    assert by_id["run:draft"]["status"] == "completed"
    assert by_id["run:review"]["status"] == "rejected"
    assert by_id["run:review"]["detail"] == "Missing comparison"
    assert by_id["run:deliver"]["status"] == "not_run"


def test_missing_legacy_history_remains_unknown_not_zero_work() -> None:
    rows = execution_view(
        {"flow": "deep-research", "status": "flow_error", "result": "lock failure"}, []
    )
    assert [(row["label"], row["status"]) for row in rows] == [
        ("Started", "completed"),
        ("Task history incomplete", "unknown"),
        ("Stopped", "failed"),
        ("Draft report", "not_run"),
        ("Report review", "not_run"),
        ("Save report", "not_run"),
    ]


def test_saved_receipt_is_preserved_even_if_flow_later_failed() -> None:
    rows = execution_view(
        {
            "flow": "deep-research",
            "status": "flow_error",
            "events": {"s": {"event": "report_persistence", "status": "verified"}},
        },
        [],
    )
    assert rows[-1]["status"] == "completed"


def test_unrecognized_flow_keeps_actual_calls_and_stopping_point() -> None:
    rows = execution_view(
        {
            "flow": "custom",
            "status": "flow_stall",
            "result": {"step": "publish", "message": "Denied"},
        },
        [{"invocation": "a", "status": "failed"}],
    )
    assert [(row["label"], row["status"]) for row in rows][-2:] == [
        ("Stopped at publish", "failed"),
        ("Result", "not_run"),
    ]


def test_published_workflow_keeps_its_declared_graph() -> None:
    assert execution_view({"view": {"steps": []}}, []) == []


def test_recorded_output_rejection_does_not_become_successful_audit() -> None:
    rows = execution_view(
        {
            "flow": "deep-research",
            "status": "flow_stall",
            "events": {
                "repair": {
                    "event": "critic_audit_repair",
                    "critic": "tc",
                    "problems": ["Invalid requirement ID"],
                },
            },
        },
        [
            {
                "invocation": "critic",
                "kind": "subagent",
                "target": "critic",
                "handle": "tc",
                "status": "completed",
            }
        ],
    )
    row = next(row for row in rows if row["id"] == "critic")
    assert row["status"] == "output_rejected"
    assert "child returned" in row["detail"]
    assert "Invalid requirement ID" in row["detail"]


def test_planned_direction_without_acceptance_is_unknown_not_invented_execution() -> None:
    rows = execution_view(
        {
            "flow": "deep-research",
            "status": "flow_error",
            "steps": {
                "p": {
                    "name": "DeepResearch._plan",
                    "result": {
                        "directions": [
                            {"question": "First?"},
                            {"question": "Second?"},
                        ]
                    },
                },
            },
        },
        [
            {
                "invocation": "run:subagent:key=research:d2:hash:0",
                "target": "researcher",
                "status": "completed",
                "recovered": True,
            }
        ],
    )
    assert (
        next(row for row in rows if row["id"] == "run:subagent:key=research:d2:hash:0")["label"]
        == "researcher 2"
    )
    missing = next(row for row in rows if row["id"] == "planned:key=research:d1")
    assert missing["status"] == "unknown"
    assert missing["after"] == ["run:plan"]
    assert "First?" in missing["detail"]


def test_recovered_children_do_not_claim_parent_waited_for_them() -> None:
    rows = execution_view(
        {"flow": "deep-research", "status": "flow_error"},
        [
            {
                "invocation": "lost",
                "target": "researcher",
                "status": "completed",
                "recovered": True,
                "returned_after_stop": True,
            }
        ],
    )
    by_id = {row["id"]: row for row in rows}
    assert by_id["lost"]["after"] == ["run:start"]
    assert by_id["lost"]["status"] == "returned_after_stop"
    assert by_id["run:stop"]["after"] == ["run:start"]
    assert "cleanup did not retire" in by_id["lost"]["detail"]


def test_reconciliation_joins_exact_run_and_owner_only() -> None:
    from clio_agent.flows.run_view import flow_run

    root = SimpleNamespace(
        handle="root",
        owner=SimpleNamespace(human_id="h", parent_handle=""),
        created_at="2026-10-25T08:00:00+00:00",
        terminal_at="2026-10-25T10:00:00+00:00",
    )

    def child(handle: str, parent: str, human: str, invocation: str) -> Any:
        return SimpleNamespace(
            handle=handle,
            created_at="2026-10-25T09:00:00+00:00",
            owner=SimpleNamespace(parent_handle=parent, human_id=human),
            invocation_id=invocation,
            label="researcher",
            status="completed",
            terminal_at="2026-10-25T10:01:00+00:00",
        )

    tasks = {
        "root": root,
        "match": child("match", "root", "h", "run:subagent:a"),
        "sibling": child("sibling", "other", "h", "run:subagent:b"),
        "human": child("human", "root", "other", "run:subagent:c"),
        "restart": child("restart", "root", "h", "run2:subagent:d"),
    }

    class History:
        def workflow_checkpoint(self, handle: str) -> Any:
            return SimpleNamespace(content="cp")

        def read_content(self, reference: str) -> dict[str, Any]:
            return {"flow": "deep-research", "status": "flow_error", "run_id": "run"}

        def task_snapshot(self, handle: str) -> Any:
            return tasks.get(handle)

        def descendant_handles(self, handle: str) -> set[str]:
            return set(tasks)

    result = flow_run(History(), "root", reconcile_children=True)
    assert result is not None
    assert [row["handle"] for row in result["calls"]] == ["match"]
    assert result["calls"][0]["returned_after_stop"] is True
