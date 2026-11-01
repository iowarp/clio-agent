"""``run_code``: a real kernel process, dispatch through the agent's tools, grants."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import pytest

from clio_agent.gact.code_mode import dispatch as dispatch_module
from clio_agent.gact.code_mode import grants, kernels
from clio_agent.gact.code_mode import tool as tool_module
from clio_agent.gact.code_mode.presentation import present, starting
from clio_agent.gact.code_mode.stubs import index_text, stub_table, tool_group
from clio_agent.gact.runtime.permission_actions import ALLOW_SCRIPT, allows, offered
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.tool_observation import RESULT_SINK

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


def _agent() -> Any:
    tools = {
        "records_list_records": dspy.Tool(
            _list_records, name="records_list_records", desc="List record ids, 5 per page."
        ),
        "records_get_record": dspy.Tool(
            _get_record, name="records_get_record", desc="One record with its reading."
        ),
        "fs_stat": dspy.Tool(_mcp_shaped, name="fs_stat", desc="Stat a file."),
        "submit": dspy.Tool(lambda answer: answer, name="submit", desc="Finish."),
    }
    return SimpleNamespace(
        tools=tools, _clio_tool_servers=frozenset({"records", "fs"}), _clio_deferred_catalog=None
    )


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

    def test_mcp_calls_return_structured_content(self, workspace: Path) -> None:
        outcome = _run("(await tools.fs.stat(path='a.txt'))['lines']")
        assert outcome["value"] == "3"

    def test_control_tools_and_unknown_names_are_refused(self, workspace: Path) -> None:
        outcome = _run(
            "try:\n    await tools.call('submit', answer='x')\n"
            "except ToolError as exc:\n    print(exc)"
        )
        assert "No tool named 'submit'" in outcome["output"]

    def test_the_nested_call_budget(self, workspace: Path, monkeypatch: Any) -> None:
        monkeypatch.setattr(dispatch_module, "max_nested_calls", lambda: 2)
        code = (
            "out = []\n"
            "for i in ('r1', 'r2', 'r3'):\n"
            "    try:\n        await tools.records.get_record(record_id=i); out.append('ok')\n"
            "    except ToolError as exc:\n        out.append(str(exc)[:30])\n"
            "out"
        )
        value = _run(code)["value"]
        assert value.startswith("['ok', 'ok', 'This script reached its limit")

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

    def test_a_timeout_stops_the_kernel_and_the_next_script_starts_fresh(
        self, workspace: Path, monkeypatch: Any
    ) -> None:
        assert _run("y = 1")["status"] == "completed"
        monkeypatch.setattr(tool_module, "call_timeout_s", lambda: 1.0)
        stopped = _run("import time\ntime.sleep(30)")
        assert stopped["status"] == "timed_out"
        monkeypatch.setattr(tool_module, "call_timeout_s", lambda: 60.0)
        again = _run("'y' in globals()")
        assert again["value"] == "False"

    def test_closing_the_session_stops_its_kernels(self, workspace: Path) -> None:
        _run("z = 1")
        kernel = kernels._KERNELS[("s", "main")]
        kernels.close_session("s")
        assert not kernel.alive() and ("s", "main") not in kernels._KERNELS


class TestStubs:
    def test_groups_by_server_and_skips_control_tools(self) -> None:
        table = stub_table(_agent().tools, ("records", "fs"))
        assert set(table["groups"]) == {"records", "fs"}
        spec = table["groups"]["records"]["get_record"]
        assert spec["tool"] == "records_get_record"
        assert spec["signature"] == "get_record(record_id: string)"
        assert "submit" not in json.dumps(table)
        assert "- tools.records.{get_record, list_records}" in index_text(table)

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
