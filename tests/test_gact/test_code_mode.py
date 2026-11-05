"""``run_code``: a real kernel process, dispatch through the agent's tools, grants."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import pytest

from clio_agent.gact.agents.tool_settings import apply_tool_settings
from clio_agent.gact.code_mode import dispatch as dispatch_module
from clio_agent.gact.code_mode import grants, kernels
from clio_agent.gact.code_mode import tool as tool_module
from clio_agent.gact.code_mode.presentation import present, starting
from clio_agent.gact.code_mode.stubs import index_text, stub_table, tool_group
from clio_agent.gact.runtime.permission_actions import ALLOW_SCRIPT, allows, offered
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.tool_observation import RESULT_SINK
from clio_agent.tools.tool_settings import CALLERS_ATTR

_READINGS = {f"r{i}": (-1.5 if i % 4 == 0 else float(i)) for i in range(1, 13)}


def _list_records(page: int = 1) -> str:
    ids = sorted(_READINGS)
    return json.dumps({"ids": ids[(page - 1) * 5 : page * 5], "pages": 3})


def _get_record(record_id: str) -> str:
    if record_id not in _READINGS:
        raise KeyError(f"no record {record_id}")
    return json.dumps({"id": record_id, "reading": _READINGS[record_id]})


def _mcp_shaped(path: str) -> str:
    """Stands in for an MCP call: the observer sees structured content, the model text."""
    sink = RESULT_SINK.get()
    if sink is not None:
        sink.append(
            (
                None,
                {
                    "content": [{"type": "text", "text": "a summary"}],
                    "structuredContent": {"path": path, "lines": 3},
                },
            )
        )
    return "a summary"


def _mcp_failed(path: str) -> str:
    """Stands in for an MCP call the server rejects: an error result with its reason."""
    sink = RESULT_SINK.get()
    reason = "content: Missing required argument"
    if sink is not None:
        sink.append(
            (
                "tool_result_is_error",
                {"content": [{"type": "text", "text": reason}], "isError": True},
            )
        )
    return reason


def _finish(answer: str) -> str:
    return answer


setattr(_finish, CALLERS_ATTR, "direct")  # as core's submit declares itself


def _agent() -> Any:
    tools = {
        "records_list_records": dspy.Tool(
            _list_records, name="records_list_records", desc="List record ids, 5 per page."
        ),
        "records_get_record": dspy.Tool(
            _get_record, name="records_get_record", desc="One record with its reading."
        ),
        "fs_stat": dspy.Tool(_mcp_shaped, name="fs_stat", desc="Stat a file."),
        "fs_touch": dspy.Tool(_mcp_failed, name="fs_touch", desc="Touch a file."),
        "submit": dspy.Tool(_finish, name="submit", desc="Finish."),
    }
    agent = SimpleNamespace(
        tools=tools, _clio_tool_servers=frozenset({"records", "fs"}), _clio_deferred_catalog=None
    )
    apply_tool_settings(agent, SimpleNamespace(tools=[], mcp_servers={}))
    return agent


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    with tool_workspace_context(str(tmp_path)):
        yield tmp_path
    kernels.close_session("s")


def _run(code: str, scope: str = "main") -> dict[str, Any]:
    return tool_module.run_script(_agent(), code, session_id="s", scope=scope)


@pytest.mark.timeout(120)  # starts real kernel processes
class TestKernel:
    def test_a_script_gathers_calls_and_prints_a_summary(self, workspace: Path) -> None:
        code = (
            "import asyncio\n"
            "ids = []\n"
            "for page in (1, 2, 3):\n"
            "    ids += (await tools.records.list_records(page=page))['ids']\n"
            "rows = await asyncio.gather(*(tools.records.get_record(record_id=i) for i in ids))\n"
            "neg = sorted(r['id'] for r in rows if r['reading'] < 0)\n"
            "print('negative:', neg)\n"
            "len(neg)"
        )
        outcome = _run(code)
        assert outcome["status"] == "completed", outcome
        assert outcome["output"].strip() == "negative: ['r12', 'r4', 'r8']"
        assert outcome["value"] == "3"
        assert (outcome["calls"], outcome["failed_calls"]) == (15, 0)
        text = tool_module.model_text(outcome)
        assert "negative:" in text and "Value: 3" in text and "15 tool calls" in text

    def test_a_script_reads_and_parses_workspace_files_itself(self, workspace: Path) -> None:
        """The kernel is ordinary Python that reads the workspace: one script parses many
        files in one process (what the run_code description tells the agent)."""
        pkg = workspace / "pkg"
        pkg.mkdir()
        for i in range(30):
            (pkg / f"m{i}.py").write_text(f'"""Module {i}."""\n', encoding="utf-8")
        result = _run(
            "import ast, pathlib\n"
            f"root = pathlib.Path({str(pkg)!r})\n"
            "docs = {p.stem: ast.get_docstring(ast.parse(p.read_text())) for p in root.glob('*.py')}\n"
            "print(len(docs), docs['m7'])\n"
        )
        assert "30 Module 7." in str(result)

    def test_state_persists_between_scripts_in_a_scope_only(self, workspace: Path) -> None:
        assert _run("x = 41")["status"] == "completed"
        assert _run("x + 1")["value"] == "42"
        other = _run("x", scope="child")
        assert other["status"] == "failed" and "NameError" in other["error"]

    def test_a_failed_call_raises_in_the_script(self, workspace: Path) -> None:
        code = (
            "try:\n"
            "    await tools.records.get_record(record_id='nope')\n"
            "except ToolError as exc:\n"
            "    print('caught', exc)\n"
        )
        outcome = _run(code)
        assert "caught KeyError" in outcome["output"]
        assert outcome["failed_calls"] == 1

    def test_a_failed_mcp_call_tells_the_script_why(self, workspace: Path) -> None:
        """The script's ``ToolError`` carries the result's own text, not only its code, so
        a script that guessed an argument name can correct itself (qualification T8b).
        SABOTAGE: set ``error = str(sink_error)`` in ``Dispatcher._run`` and this fails."""
        outcome = _run(
            "try:\n    await tools.fs.touch(path='a.txt')\n"
            "except ToolError as exc:\n    print('caught', exc)"
        )
        assert (
            "caught tool_result_is_error: content: Missing required argument" in (outcome["output"])
        )

    def test_mcp_calls_return_structured_content(self, workspace: Path) -> None:
        outcome = _run("(await tools.fs.stat(path='a.txt'))['lines']")
        assert outcome["value"] == "3"

    def test_direct_only_blocked_and_unknown_names_are_refused_apart(self, workspace: Path) -> None:
        """A5: a direct-only tool says to call it directly, a blocked one gives the typed
        permission refusal, and only an unknown name is "No tool named". SABOTAGE: drop
        the direct-only or ``refuse`` branch of ``Dispatcher._refusal`` -> red."""
        agent = _agent()
        agent.tools["records_drop"] = dspy.Tool(_finish, name="records_drop", desc="Drop.")
        deny = SimpleNamespace(tools=[{"name": "records_drop", "permission": "deny"}])
        apply_tool_settings(agent, SimpleNamespace(mcp_servers={}, **vars(deny)))
        code = (
            "for name in ('submit', 'records_drop', 'nothing_here'):\n"
            "    try:\n        await tools.call(name, answer='x')\n"
            "    except ToolError as exc:\n        print(exc)"
        )
        lines = tool_module.run_script(agent, code, session_id="s", scope="main")["output"]
        direct, blocked, unknown = lines.strip().splitlines()
        assert direct == "submit cannot be called from a script: call submit directly, " + (
            "not from a script."
        )
        assert "records_drop is blocked by a permission setting (tool_permission_deny)" in blocked
        assert unknown.startswith("No tool named 'nothing_here' can be called from a script")

    def test_a_script_search_takes_its_limit(self, workspace: Path) -> None:
        """A2: ``tools.search(query, limit)`` returns at most ``limit`` tools, bounded by
        ``tools.search_max``. SABOTAGE: search with a fixed 5 again -> two results -> red."""
        outcome = _run(
            "(len(await tools.search('record', limit=1)), "
            "len(await tools.search('record', limit=99)))"
        )
        assert outcome["value"] == "(1, 2)", outcome

    def test_no_count_ends_a_script(self, workspace: Path) -> None:
        """CT3: a script may make more calls than the 500 the old budget allowed.

        SABOTAGE: restore a budget of 500 in the dispatcher -> the 501st call raises -> red."""
        code = (
            "import asyncio\n"
            "rows = await asyncio.gather(*(tools.records.get_record(record_id='r1') "
            "for _ in range(520)))\n"
            "len(rows)"
        )
        outcome = _run(code)
        assert outcome["status"] == "completed", outcome
        assert outcome["value"] == "520" and outcome["calls"] == 520

    def test_tasks_a_failed_script_left_running_stop_with_it(self, workspace: Path) -> None:
        # A failed gather leaves its other coroutines running on the kernel's loop;
        # they must not keep calling tools against the next script's budget.
        code = (
            "import asyncio\n"
            "async def keep_reading():\n"
            "    for _ in range(200):\n"
            "        await tools.records.get_record(record_id='r1')\n"
            "await asyncio.gather(keep_reading(), tools.records.get_record(record_id='nope'))"
        )
        failed = _run(code)
        assert failed["status"] == "failed" and "ToolError" in failed["error"]
        stopped = "1 task this script started was still running and stopped with it"
        assert stopped in failed["output"]
        after = _run("await tools.records.get_record(record_id='r2')")
        assert after["status"] == "completed"
        assert after["calls"] == 1

    def test_no_clock_ends_a_script(self) -> None:
        """CT3: nothing in run_code stops a script for running long; the person cancels it."""
        assert not hasattr(tool_module, "call_timeout_s")
        assert not hasattr(dispatch_module, "max_nested_calls")

    def test_the_output_limit_reaches_the_kernel(self, workspace: Path, monkeypatch: Any) -> None:
        # code.max_output_bytes is resolved where the kernel starts and passed to it.
        monkeypatch.setenv("CLIO_CODE_MAX_OUTPUT_BYTES", "10")
        outcome = _run("print('x' * 100)", scope="limited")
        assert outcome["status"] == "completed", outcome
        assert outcome["output"].startswith("x" * 10 + "\n[output cut at 10 of 101 bytes]")

    def test_closing_the_session_stops_its_kernels(self, workspace: Path) -> None:
        _run("z = 1")
        kernel = kernels._KERNELS[("s", "main")]
        kernels.close_session("s")
        assert not kernel.alive() and ("s", "main") not in kernels._KERNELS


class TestStubs:
    def test_groups_by_server_and_skips_direct_tools(self) -> None:
        agent = _agent()
        table = stub_table(agent.tools, ("records", "fs"), direct_only=agent._clio_direct_only)
        assert set(table["groups"]) == {"records", "fs"}
        spec = table["groups"]["records"]["get_record"]
        assert spec["tool"] == "records_get_record"
        assert spec["signature"] == "get_record(record_id: string)"
        assert "submit" not in json.dumps(table)
        assert "- tools.records.{get_record, list_records}" in index_text(table)

    def test_help_gives_the_whole_description(self) -> None:
        description = "Fetch one record. " + "Field notes: units are SI. " * 200 + "END."
        tool = dspy.Tool(_get_record, name="records_get_record", desc=description)
        table = stub_table({"records_get_record": tool}, ("records",))
        assert table["groups"]["records"]["get_record"]["doc"] == description

    def test_server_names_become_identifiers(self) -> None:
        assert tool_group("synthetic-home_climate_list", ["synthetic-home"]) == (
            "synthetic_home",
            "climate_list",
        )
        assert tool_group("ask_user", ["synthetic-home"]) == ("", "ask_user")


class TestGrants:
    def test_modes_that_cover_nested_calls(self) -> None:
        assert not grants.covers("auto-edits")  # no script running
        token = grants.bind_grant(grants.ScriptGrant("call_1", "print(1)"))
        try:
            assert grants.covers("auto-edits") and grants.covers("spotter-watcher")
            assert not grants.covers("ask")
            fields = grants.row_fields(["allow", "deny"])
            assert fields["script"] == {"call_id": "call_1", "code": "print(1)"}
            assert fields["actions"] == ["allow", "deny", ALLOW_SCRIPT]
            with grants.question():
                assert grants.active_grant().waiting
            grants.answered({"action": ALLOW_SCRIPT})
            assert grants.covers("ask")
        finally:
            grants.reset_grant(token)

    def test_the_covering_modes_and_script_length_are_configured(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("CLIO_CODE_COVERING_POSTURES", "ask")
        monkeypatch.setenv("CLIO_CODE_MAX_SCRIPT_CHARS", "3")
        token = grants.bind_grant(grants.ScriptGrant("call_1", "print(1)"))
        try:
            assert grants.covers("ask") and not grants.covers("auto-edits")
            assert grants.row_fields(["allow"])["script"]["code"] == "pri"
        finally:
            grants.reset_grant(token)

    def test_allow_script_is_valid_only_where_offered(self) -> None:
        assert allows(ALLOW_SCRIPT) and not allows("ask")
        assert offered({}, "allow") and not offered({}, ALLOW_SCRIPT)
        assert offered({"actions": ["allow", ALLOW_SCRIPT]}, ALLOW_SCRIPT)
        folder = {"kind": "folder_access", "actions": ["deny", "allow_workspace"]}
        assert offered(folder, "allow_workspace") and not offered(folder, "allow")


class TestPresentation:
    def test_finished_script(self) -> None:
        row = {
            "code": "print(1)",
            "status": "completed",
            "output": "1\n",
            "value": None,
            "calls": 14,
            "failed_calls": 1,
            "call_tools": ["records_get_record"],
        }
        view = present({}, "", row)
        assert view["summary"] == "14 calls, 1 failed"
        assert view["blocks"][0]["language"] == "python"
        assert "status" not in view
        # The calls are nested rows; the result does not list their tool identifiers.
        assert all("records_get_record" not in str(b.get("text")) for b in view["blocks"])

    def test_timed_out_and_cancelled(self) -> None:
        timed = present({}, "", {"code": "x", "status": "timed_out", "error": "late", "calls": 0})
        assert timed["status"] == "failed" and timed["summary"].startswith("Stopped")
        cancelled = present({}, "", {"code": "x", "status": "cancelled", "calls": 2})
        assert cancelled["status"] == "interrupted"
        assert starting({"code": "x = 1"})["blocks"][0]["text"] == "x = 1"


def test_nested_calls_carry_their_parent_into_the_live_and_v3_views() -> None:
    from clio_agent.gact.events import Event
    from clio_agent.gact.protocol.v3.event import _tool_started
    from clio_agent.gact.tool_call_nesting import PARENT_CALL_ID, live_call_metadata

    assert "parent_call_id" not in live_call_metadata("2026-11-01T00:00:00Z", None)
    token = PARENT_CALL_ID.set("call_run")
    try:
        metadata = live_call_metadata("2026-11-01T00:00:00Z", "step_1")
    finally:
        PARENT_CALL_ID.reset(token)
    assert metadata["parent_call_id"] == "call_run"
    assert metadata["thought_step_id"] == "step_1"
    payload = {"call_id": "call_child", "tool": "fs_read_file", "parent_call_id": "call_run"}
    event = Event(type="tool.call.started", session_id="s", payload=payload)
    projection = _tool_started(event, payload, None)
    assert projection.payload["parent_call_id"] == "call_run"


def test_a_nested_calls_completion_carries_its_parent_too() -> None:
    """A background script's calls are recorded behind the call (CT5), so a live view can
    meet a call first through its completion; that event must name the parent as well,
    or the call shows as a row of its own (CK21: stray "Created version" rows).

    SABOTAGE: drop ``parent_call_id`` from ``_tool_completed`` -> red."""
    from clio_agent.gact.events import Event
    from clio_agent.gact.protocol.v3.event import _tool_completed

    payload = {"call_id": "call_child", "tool": "create_artifact", "ok": True}
    payload["parent_call_id"] = "call_run"
    event = Event(type="tool.call.completed", session_id="s", payload=payload)
    projection = _tool_completed(event, payload, None)
    assert projection.payload["parent_call_id"] == "call_run"
    assert (
        "parent_call_id"
        not in _tool_completed(event, {**payload, "parent_call_id": ""}, None).payload
    )


def test_plan_mode_runs_scripts_only_behind_an_enforced_fence(monkeypatch: Any) -> None:
    """The sandbox is what keeps a script read-only in plan mode; without it, refuse typed."""
    from clio_agent.runtime import sandbox

    sessions = {"plan": SimpleNamespace(mode="plan"), "edit": SimpleNamespace(mode="edit")}
    app = SimpleNamespace(state=SimpleNamespace(sessions=SimpleNamespace(get=sessions.get)))
    monkeypatch.setattr(sandbox, "current_state", lambda: None)
    with pytest.raises(tool_module.CodeError) as refused:
        tool_module._plan_mode_refusal(app, "plan")
    assert refused.value.reason == tool_module.CODE_PLAN_MODE_UNCONFINED
    tool_module._plan_mode_refusal(app, "edit")  # outside plan mode the fence is not required
    enforced = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_LANDLOCK, active=True, reason=sandbox.REASON_FENCE_ACTIVE
    )
    monkeypatch.setattr(sandbox, "current_state", lambda: enforced)
    tool_module._plan_mode_refusal(app, "plan")


def test_a_plan_mode_kernel_writes_only_scratch_and_temp(monkeypatch: Any, tmp_path: Path) -> None:
    """In plan mode the kernel's write roots leave out the workspace, and a kernel from the
    other mode is replaced rather than reused.
    SABOTAGE: start every kernel with ``effective_write_roots`` (ignore ``read_only``), or
    drop the ``read_only`` comparison in ``kernel_for``, and this fails."""
    from clio_agent.gact.code_mode import kernels
    from clio_agent.runtime import sandbox, sandbox_roots, session_scratch

    scratch = tmp_path / "scratch"
    monkeypatch.setattr(session_scratch, "active_scratch_dir", lambda: scratch)
    monkeypatch.setattr(session_scratch, "system_temp_roots", lambda: (tmp_path / "tmp",))
    assert sandbox_roots.read_only_write_roots() == (scratch, tmp_path / "tmp")

    seen: list[tuple[Path, ...]] = []

    def fake_wrap(command: str, args: list[str], **kwargs: Any) -> Any:
        seen.append(tuple(kwargs["write_roots"]))
        raise RuntimeError("stop before spawning")

    monkeypatch.setattr(sandbox, "wrap_confined", fake_wrap)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    for read_only in (True, False):
        with pytest.raises(RuntimeError):
            kernels.kernel_for("sess_plan", "scope", workspace, read_only=read_only)
    kernels.close_session("sess_plan")
    plan_roots, edit_roots = seen
    assert workspace not in plan_roots and set(plan_roots) == {scratch, tmp_path / "tmp"}
    assert workspace in edit_roots

    stopped: list[str] = []
    old = kernels.Kernel(("sess_x", "scope"), workspace, read_only=False)
    monkeypatch.setattr(old, "alive", lambda: True)
    monkeypatch.setattr(old, "stop", lambda reason: stopped.append(reason))
    kernels._KERNELS[("sess_x", "scope")] = old
    try:
        with pytest.raises(RuntimeError):
            kernels.kernel_for("sess_x", "scope", workspace, read_only=True)
        assert stopped == ["replaced_mode_changed (read_only=True)"]
    finally:
        kernels._KERNELS.pop(("sess_x", "scope"), None)


def test_a_background_launch_reads_as_a_started_task_not_as_a_script_without_calls() -> None:
    """The launch row links to the task's row in Work by its handle.

    SABOTAGE: drop the background branch in ``present`` -> "No tool calls". Drop the
    ``open-task`` link -> red."""
    view = present(
        {"code": "while True:\n    pass", "background": True, "label": "Counter watch"},
        "Started",
        {"handle": "task_abc", "status": "working"},
    )
    assert view["summary"] == "Started in the background: Counter watch"
    blocks = {block["id"]: block for block in view["blocks"]}
    assert "task_abc" in blocks["task"]["text"]
    assert blocks["open-task"] | {"label": ""} == {
        "id": "open-task",
        "type": "link",
        "target": "work",
        "uri": "task_abc",
        "label": "",
    }
    unnamed = present({"code": "\nimport time\n", "background": True}, "", {"handle": "t"})
    assert unnamed["summary"] == "Started in the background: import time"


def test_a_finished_script_waits_for_the_calls_it_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calls queued behind a busy pool when the script ends still run and are answered;
    only a stopped script drops them (``close(finished=False)``).

    SABOTAGE: close a finished script's dispatcher with ``cancel_futures`` -> the queued
    call never runs -> red."""
    import contextvars
    import time

    from clio_agent.gact.code_mode.dispatch import Dispatcher

    monkeypatch.setenv("CLIO_TOOLS_CALL_WORKERS", "1")
    ran: list[str] = []

    def slow(args: Any) -> str:
        time.sleep(0.3)
        ran.append(str(args["name"]))
        return "ok"

    dispatcher = Dispatcher({}, (), contextvars.copy_context(), lambda q, n: [], {"__w__": slow})
    replies: list[dict[str, Any]] = []
    for call_id, name in ((1, "first"), (2, "queued")):
        dispatcher.submit({"id": call_id, "tool": "__w__", "args": {"name": name}}, replies.append)
    dispatcher.close(finished=True)
    assert ran == ["first", "queued"]
    assert sorted(r["id"] for r in replies if r["ok"]) == [1, 2]


def test_a_call_that_raises_outside_its_own_handling_still_answers_the_script() -> None:
    """A11: a call (or a host call) whose worker raises before its own try (entering its
    server's gate) answers the script with the error instead of leaving it waiting.
    SABOTAGE: settle with ``f.result()`` again -> no reply -> red."""
    import contextvars

    from clio_agent.gact.code_mode.dispatch import Dispatcher

    def gone(args: Any) -> str:
        raise SystemExit("host call gone")

    tools = {"records_get_record": object()}
    dispatcher = Dispatcher(tools, (), contextvars.copy_context(), lambda q, n: [], {"__h__": gone})

    def broken(tool: str, args: dict[str, Any]) -> Any:
        raise RuntimeError("gate unavailable")

    dispatcher._run = broken  # type: ignore[method-assign]
    replies: list[dict[str, Any]] = []
    dispatcher.submit({"id": 1, "tool": "records_get_record", "args": {}}, replies.append)
    dispatcher.submit({"id": 2, "tool": "__h__", "args": {}}, replies.append)
    dispatcher.close(finished=True)
    by_id = {r["id"]: r for r in replies}
    assert by_id[1]["ok"] is False and by_id[1]["error"] == "RuntimeError: gate unavailable"
    assert by_id[2]["ok"] is False and "host call gone" in by_id[2]["error"]
    assert [(r.tool, r.ok) for r in dispatcher.records] == [("records_get_record", False)]


def test_a_refused_script_says_why_the_gate_refused_it() -> None:
    """A3: the gate's own deny message (a deny policy, the plan ACL, a read-only session)
    reaches the model with a typed reason; only a bare deny reads as not approved.
    SABOTAGE: raise the fixed "not approved" text for every refusal -> red."""
    from clio_agent.gact.permission_gate import DenyDecision

    def app(decision: Any) -> Any:
        gate = lambda name, args, context: decision  # noqa: E731
        return SimpleNamespace(state=SimpleNamespace(pending_permission_gate=gate))

    tool_module._approve(app("allow"), "print(1)")
    acl = "run_code is not available in this read-only session."
    with pytest.raises(PermissionError) as denied:
        tool_module._approve(app(DenyDecision(acl)), "print(1)")
    assert str(denied.value).startswith(acl)
    assert tool_module.CODE_SCRIPT_DENIED in str(denied.value)
    assert "not approved" not in str(denied.value)
    with pytest.raises(PermissionError) as declined:
        tool_module._approve(app("deny"), "print(1)")
    assert "not approved, or a permission rule denies run_code" in str(declined.value)
    assert tool_module.CODE_SCRIPT_NOT_ALLOWED in str(declined.value)
