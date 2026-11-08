"""``delegate_to_clio_coder``: its contract, withholding and human presentation.

The showcase rows are the real result shape (``DelegationResult.to_dict`` plus the tool's
bounded fields): a small verified change, a large truncated diff, a no-op, a run Clio Coder
reports as failed while the parent's check passes, a failed check, and the running state.
"""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.gact import clio_coder_tool as tool
from clio_agent.gact.agents import native_tool_registry as registry

ARGS = {"task": "Fix add() in calc.py so add(2, 3) == 5", "follow_ups": ["Now add sub(a, b)"]}
DIFF = "diff --git a/calc.py b/calc.py\n-    return a - b\n+    return a + b\n"


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "surface": "acp",
        "outcome": "succeeded",
        "outcome_detail": "end_turn",
        "text": "Fixed `add()`; the acceptance check passes.",
        "changed_files": ["calc.py"],
        "diff": DIFF,
        "diff_truncated": False,
        "branch": "clio-coder/20261108-0001-abcdef",
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "workspace": "/state/clio-coder/worktrees/20261108-0001-abcdef",
        "verification": {
            "command": ["python3", "-c", "import calc; assert calc.add(2, 3) == 5"],
            "exit_code": 0,
            "output_tail": "",
        },
        "verified": True,
    }
    return {**row, **overrides}


def _blocks(presentation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {block["id"]: block for block in presentation["blocks"]}


def test_a_verified_change_shows_assignment_answer_diff_check_and_branch() -> None:
    view = tool.clio_coder_presentation(ARGS, None, _row())
    blocks = _blocks(view)
    assert view["action"] == "Delegate to Clio Coder"
    assert (view["status"], view["summary"]) == ("completed", "1 file changed, check passed")
    assert blocks["assignment"]["text"] == ARGS["task"]
    assert blocks["follow-up-0"]["text"] == "Then: Now add sub(a, b)"
    assert blocks["answer"]["type"] == "markdown" and blocks["diff"]["text"] == DIFF
    assert blocks["verification"]["exit_code"] == 0
    assert "import calc" in blocks["verification"]["command"]
    assert blocks["branch"]["text"].startswith(
        "Committed 0123456789ab on branch clio-coder/20261108-0001-abcdef"
    )


def test_a_large_diff_is_bounded_and_points_at_the_branch() -> None:
    files = [f"src/very/long/package/name/module_{index:03d}.py" for index in range(240)]
    diff = "".join(f"diff --git a/{name} b/{name}\n+x\n" for name in files) * 20
    bounded, truncated = tool._bounded(diff, tool.DIFF_LIMIT)
    view = tool.clio_coder_presentation(
        ARGS, None, _row(changed_files=files, diff=bounded, diff_truncated=truncated)
    )
    blocks = _blocks(view)
    assert truncated and len(blocks["diff"]["text"]) == tool.DIFF_LIMIT
    assert view["summary"].startswith("240 files changed")
    assert "full change is on branch clio-coder/" in blocks["diff-truncated"]["text"]


def test_no_change_and_no_check_say_so() -> None:
    view = tool.clio_coder_presentation(
        {"task": "Explain calc.py"}, None, _row(changed_files=[], diff="", verification=None)
    )
    assert (view["status"], view["summary"]) == ("completed", "0 files changed")
    assert _blocks(view)["no-change"]["text"] == "No files were changed."
    assert "verification" not in _blocks(view)


def test_clio_coder_failure_is_a_failure_even_when_the_parent_check_passes() -> None:
    view = tool.clio_coder_presentation(
        ARGS, None, _row(surface="headless", outcome="failed", outcome_detail="limitation")
    )
    assert view["status"] == "failed"
    assert view["summary"] == "1 file changed, check passed, Clio Coder failed (limitation)"


def test_a_failed_or_timed_out_check_is_unaccepted() -> None:
    check = {"command": ["pytest", "-q"], "exit_code": 1, "output_tail": "1 failed"}
    failed = tool.clio_coder_presentation(ARGS, None, _row(verification=check, verified=False))
    assert (failed["status"], failed["summary"]) == ("failed", "1 file changed, check failed")
    assert _blocks(failed)["verification"]["text"] == "1 failed"
    hung = {"command": ["pytest"], "exit_code": None, "output_tail": ""}
    timed_out = tool.clio_coder_presentation(
        ARGS, None, _row(outcome="timed_out", verification=hung, verified=False)
    )
    assert _blocks(timed_out)["verification"]["timed_out"] is True
    assert timed_out["summary"].endswith("Clio Coder timed_out (end_turn)")


def test_running_state_shows_the_assignment_and_unreadable_results_are_disclosed() -> None:
    start = tool.clio_coder_start_presentation(ARGS)
    assert [block["id"] for block in start["blocks"]] == ["assignment", "follow-up-0"]
    assert tool.clio_coder_presentation(ARGS, "not a row", None)["diagnostic"] == "invalid"


async def test_arguments_are_validated_before_any_instance_starts() -> None:
    with pytest.raises(ValueError, match="nonempty"):
        await tool.delegate_to_clio_coder("  ")
    with pytest.raises(ValueError, match="timeout_s"):
        await tool.delegate_to_clio_coder("x", timeout_s=5)


def test_delegated_autonomy_inherits_the_conversation_and_never_exceeds_it() -> None:
    from types import SimpleNamespace

    def ceiling(mode: str = "edit", approval_mode: str = "ask") -> str:
        return tool.autonomy_ceiling(SimpleNamespace(mode=mode, approval_mode=approval_mode))

    assert ceiling() == "default"
    assert ceiling(approval_mode="auto-edits") == "default"
    assert ceiling(approval_mode="bypass") == "yolo"
    assert ceiling(mode="plan", approval_mode="bypass") == "read-only"
    assert tool.resolve_autonomy("inherit", "yolo") == "yolo"
    assert tool.resolve_autonomy("read-only", "default") == "read-only"
    with pytest.raises(ValueError, match="exceeds this conversation's autonomy"):
        tool.resolve_autonomy("yolo", "default")
    with pytest.raises(ValueError, match="exceeds"):
        tool.resolve_autonomy("default", "read-only")
    with pytest.raises(ValueError, match="autonomy must be one of"):
        tool.resolve_autonomy("root", "yolo")


def test_the_tool_is_withheld_when_clio_coder_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool, "clio_coder_unavailable_reason", lambda: "clio_coder_not_installed")
    available, _gateway, withheld = registry.build_native_tools(
        None, ["delegate_to_clio_coder"], {}
    )
    assert withheld == {"delegate_to_clio_coder": "clio_coder_not_installed"}
    assert not available
    monkeypatch.setattr(tool, "clio_coder_unavailable_reason", lambda: None)
    available, _gateway, withheld = registry.build_native_tools(
        None, ["delegate_to_clio_coder"], {}
    )
    assert list(available) == ["delegate_to_clio_coder"] and not withheld


def test_a_limitation_is_a_delegation_result_not_a_failed_tool_call() -> None:
    from clio_agent.runtime.clio_coder import DelegationResult
    from clio_agent.tools.result_errors import structured_tool_result_error

    result = DelegationResult(
        surface="headless",
        outcome="failed",
        outcome_detail="limitation",
        error={"message": "the agent recorded an explicit limitation"},
    )
    row = tool.result_row(result, "fix add", [])
    assert "error" not in row and structured_tool_result_error(row) is None
    blocks = _blocks(tool.clio_coder_presentation(ARGS, None, row))
    assert blocks["instance-error"]["text"].endswith("explicit limitation")


def test_the_check_runs_through_the_shell_and_shows_as_written() -> None:
    argv = tool._shell_argv("cd sub && pytest -q")
    view = tool.clio_coder_presentation(
        ARGS, None, _row(verification={"command": argv, "exit_code": 0, "output_tail": ""})
    )
    assert _blocks(view)["verification"]["command"] == "cd sub && pytest -q"
