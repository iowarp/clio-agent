"""``run_clio_coder``: its contract, withholding and human presentation.

The showcase rows are the real result shape (``DelegationResult.to_dict`` plus the tool's
bounded fields): a small verified change, a large truncated diff, a no-op, a run Clio Coder
reports as failed while the parent's check passes, a failed check, and the running state.
"""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.gact import clio_coder_land as land
from clio_agent.gact import clio_coder_relay as relay
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


def test_clio_coders_own_check_runs_are_shown_but_the_parent_check_decides() -> None:
    checks = [
        {"command": "make test", "passed": True, "exitCode": 0, "ranAfterLastEdit": True},
        {"command": "make lint", "passed": False, "exitCode": 2, "timedOut": False},
    ]
    view = tool.clio_coder_presentation(
        ARGS, None, _row(child_checks=checks, verified=False, verification={"exit_code": 1})
    )
    blocks = _blocks(view)
    assert view["status"] == "failed" and view["summary"].endswith("check failed")
    assert blocks["child-check-0"]["text"] == (
        "Clio Coder's own run of `make test`: passed; it also ran it after its last edit."
    )
    assert (
        blocks["child-check-1"]["text"] == "Clio Coder's own run of `make lint`: failed (exit 2)."
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
        await tool.run_clio_coder("  ")
    with pytest.raises(ValueError, match="timeout_s"):
        await tool.run_clio_coder("x", timeout_s=5)
    with pytest.raises(ValueError, match="continue_run must be a run id"):
        await tool.run_clio_coder("x", continue_run="../other")
    with pytest.raises(ValueError, match="action must be"):
        await land.land_clio_coder("20261108-000000-abcdef", action="merge")


async def test_continue_run_resumes_the_run_it_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from types import SimpleNamespace

    (tmp_path / ".git").mkdir()
    session = SimpleNamespace(mode="edit", approval_mode="ask")
    route = SimpleNamespace(target=None, home_template=None, env={}, describe=lambda: "binding")
    seen: list[Any] = []

    async def approve(*_args: Any) -> None:
        return None

    async def resume(request: Any, **hooks: Any) -> Any:
        seen.append((request, sorted(hooks)))
        raise RuntimeError("resumed")

    app = SimpleNamespace(state=SimpleNamespace(pending_permission_gate=object()))
    monkeypatch.setattr(tool, "_active_session", lambda: (app, session, tmp_path))
    monkeypatch.setattr(tool, "resolve_route", lambda *_a, **_k: route)
    monkeypatch.setattr(tool, "_approve", approve)
    monkeypatch.setattr(tool, "continue_clio_coder_run", resume)
    with pytest.raises(RuntimeError, match="resumed"):
        await tool.run_clio_coder("also add sub", continue_run="20261108-000000-abcdef")
    ((request, hooks),) = seen
    assert request.run_id == "20261108-000000-abcdef" and request.task == "also add sub"
    assert hooks == ["on_permission", "sink"]


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
    names = ["run_clio_coder", "land_clio_coder"]
    monkeypatch.setattr(tool, "clio_coder_unavailable_reason", lambda: "clio_coder_not_installed")
    available, _gateway, withheld = registry.build_native_tools(None, names, {})
    assert withheld == dict.fromkeys(names, "clio_coder_not_installed")
    assert not available
    monkeypatch.setattr(tool, "clio_coder_unavailable_reason", lambda: None)
    available, _gateway, withheld = registry.build_native_tools(None, names, {})
    assert sorted(available) == sorted(names) and not withheld


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


RUN = "20261108-000100-abcdef"


def test_each_turn_names_its_run_turn_session_and_next_step() -> None:
    from clio_agent.runtime.clio_coder import DelegationResult
    from clio_agent.runtime.clio_coder.results import Verification

    result = DelegationResult(
        surface="acp",
        outcome="succeeded",
        session_id="s-1",
        commit="abc",
        run_id=RUN,
        turn=2,
        run_usage={"input": 1200, "output": 30, "cacheRead": 900},
        verification=Verification(["true"], 0, ""),
    )
    row = tool.result_row(result, "add sub", [])
    assert row["run"] == RUN and row["next"].startswith(f"land_clio_coder(run='{RUN}'")
    view = tool.clio_coder_presentation({"task": "add sub", "continue_run": RUN}, None, row)
    assert view["action"] == f"Continue Clio Coder run {RUN}"
    assert _blocks(view)["run"]["text"] == (
        f"Run {RUN}, turn 2, Clio Coder session s-1; run total: 1200 input tokens, 30 output tokens."
    )
    result.verification = Verification(["true"], 1, "")
    assert "continue_run=" in tool.result_row(result, "add sub", [])["next"]


OPTIONS = [
    {"optionId": "allow-once", "kind": "allow_once"},
    {"optionId": "reject-once", "kind": "reject_once"},
    {"optionId": "reject-and-stop", "kind": "reject_once"},
]
ASK = {
    "toolCall": {
        "title": "bash: rm -rf build",
        "kind": "execute",
        "rawInput": {"command": "rm -rf build"},
        "locations": [{"path": "/wt/build"}],
    },
    "options": OPTIONS,
}


@pytest.mark.parametrize(("verdict", "chosen"), [("allow", "allow-once"), ("deny", "reject-once")])
async def test_clio_coder_asks_go_through_the_conversations_own_gate(
    verdict: str, chosen: str
) -> None:
    from types import SimpleNamespace

    seen: list[tuple[str, dict, dict]] = []

    def gate(name: str, args: dict, context: dict) -> str:
        seen.append((name, dict(args), dict(context)))
        return verdict

    app = SimpleNamespace(state=SimpleNamespace(pending_permission_gate=gate))
    answer = await relay.gate_permission_relay(app, tool.TOOL_NAME, RUN)(ASK)

    # A refusal never stops Clio Coder's whole turn; it may try another way.
    assert answer == chosen
    name, args, context = seen[0]
    assert name == "run_clio_coder" and args["run"] == RUN and args["kind"] == "execute"
    assert args["input"] == '{"command": "rm -rf build"}' and args["locations"] == ["/wt/build"]
    assert context["summary"].startswith(
        f"Clio Coder (run {RUN}) asks to bash: rm -rf build (/wt/build)"
    )


async def test_a_path_scoped_deny_on_any_location_refuses_the_ask() -> None:
    from types import SimpleNamespace

    paths: list[str] = []

    def gate(name: str, args: dict, context: dict) -> str:
        paths.append(args["path"])
        return "deny" if args["path"].startswith("/wt/protected/") else "allow"

    ask = {
        **ASK,
        "toolCall": {
            **ASK["toolCall"],
            # Past the 20 locations the ask's description shows: the gate still sees every one.
            "locations": [{"path": f"/wt/build/{i}"} for i in range(25)]
            + [{"path": "/wt/protected/key"}],
        },
    }
    app = SimpleNamespace(state=SimpleNamespace(pending_permission_gate=gate))
    assert await relay.gate_permission_relay(app, tool.TOOL_NAME, RUN)(ask) == "reject-once"
    assert paths[-1] == "/wt/protected/key" and len(paths) == 26


def test_progress_streams_session_finished_tools_and_notices_once_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact import context
    from clio_agent.gact.tool_observer import _OBSERVER_CALL_IDS
    from clio_agent.tools import execution

    sent: list[dict] = []
    monkeypatch.setattr(context, "active_session_id", lambda: "sess")
    monkeypatch.setattr(
        execution, "notify_global_tool_observer", lambda *a: sent.append(a[4]) or None
    )
    token = _OBSERVER_CALL_IDS.set("call_1")
    try:
        sink = relay.progress_sink(tool.TOOL_NAME, {"task": "x"})
    finally:
        _OBSERVER_CALL_IDS.reset(token)
    for event in (
        {"kind": "session", "sessionId": "s-1", "resumed": True},
        {"kind": "tool", "name": "edit calc.py", "status": "in_progress"},
        {"kind": "tool", "name": "edit calc.py", "status": "completed"},
        {"kind": "tool", "name": "edit calc.py", "status": "completed"},
        {"kind": "text", "text": "chunk"},
        {"kind": "notice", "level": "info", "text": "checks passed"},
    ):
        sink(event)
    assert [item["message"] for item in sent] == [
        "Clio Coder session s-1 resumed",
        "Clio Coder completed: edit calc.py",
        "Clio Coder notice: checks passed",
    ]
    assert sent[0]["observer_handle"] == {"call_id": "call_1", "session_id": "sess"}


def test_landing_shows_what_was_applied_or_discarded() -> None:
    applied = land.land_presentation(
        {"run": RUN, "action": "apply"},
        None,
        {
            "action": "apply",
            "changed_files": ["calc.py"],
            "diff": DIFF,
            "commit": "0123456789abcdef",
            "branch": f"clio-coder/{RUN}",
            "checkout": "/repo",
        },
    )
    assert applied["action"] == f"Land Clio Coder run {RUN}"
    assert applied["summary"] == "1 file applied to the working tree"
    assert _blocks(applied)["landed"]["text"].startswith(
        f"Applied 0123456789ab from branch clio-coder/{RUN} to /repo (uncommitted)"
    )
    discarded = land.land_presentation(
        {"run": RUN, "action": "discard"}, None, {"action": "discard", "branch": "b"}
    )
    assert (discarded["action"], discarded["summary"]) == (
        f"Discard Clio Coder run {RUN}",
        "discarded",
    )


def test_windows_check_paths_keep_native_quotes_and_shell_operators() -> None:
    from clio_agent.runtime.clio_coder.results import command_text

    check = r'"C:\Python 3.14\python.exe" -B check.py && echo accepted'
    argv = tool._shell_argv(check, platform="win32")
    assert argv[-1] == check and argv[-2].endswith("windows_check.py")
    assert command_text(argv, platform="win32") == check
    native = [r"C:\Python 3.14\python.exe", "-B", r"C:\science data\check.py"]
    assert command_text(native, platform="win32") == (
        r'"C:\Python 3.14\python.exe" -B "C:\science data\check.py"'
    )


def test_windows_check_passes_the_original_string_to_cmd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from clio_agent.runtime.clio_coder import windows_check

    command = r'"C:\Python 3.14\python.exe" -B check.py && echo accepted'
    calls = []
    monkeypatch.setattr(windows_check.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_check.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=7),
    )
    assert windows_check.run_check(command) == 7
    assert calls[0][0] == (command,) and calls[0][1]["shell"] is True
    assert calls[0][1]["stdin"] == windows_check.subprocess.DEVNULL
