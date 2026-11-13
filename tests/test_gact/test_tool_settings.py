"""Per-tool I/O (``callers``) and default permissions (TD52-TD55)."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import pytest

from clio_agent import conf
from clio_agent.gact import context as _ctx
from clio_agent.gact.agents import tool_settings as settings
from clio_agent.gact.agents.deferred_tools import apply_disclosure
from clio_agent.gact.agents.dspy_native_tool import ClioNativeTool
from clio_agent.gact.code_mode import grants, kernels
from clio_agent.gact.code_mode import tool as tool_module
from clio_agent.gact.code_mode.stubs import index_text, stub_table
from clio_agent.gact.runtime import tool_permission_layers as layers
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.tool_settings import CALLERS_ATTR, configured_callers, configured_permissions
from tests.turn_signals import TURN_SIGNAL_BACKSTOP_S


def _records_list(page: int = 1) -> str:
    return json.dumps({"ids": [f"r{page}"]})


def _records_get(record_id: str) -> str:
    return json.dumps({"id": record_id})


def _ask(question: str) -> str:
    return question


setattr(_ask, CALLERS_ATTR, "direct")  # as core's ask_user declares itself


def _program(*, run_code: bool = True) -> Any:
    tools = {
        "records_list": dspy.Tool(_records_list, name="records_list", desc="List record ids."),
        "records_get": dspy.Tool(_records_get, name="records_get", desc="One record."),
        "ask_user": dspy.Tool(_ask, name="ask_user", desc="Ask the person."),
        "search_tools": dspy.Tool(lambda query: query, name="search_tools", desc="Find tools."),
    }
    if run_code:
        tools["run_code"] = dspy.Tool(lambda code: code, name="run_code", desc="Run Python.")
    return SimpleNamespace(tools=tools, _clio_unlisted=frozenset())


#: The plugin says ``records`` is code-only, except ``records_get``, which both may call.
_RESOLUTION = SimpleNamespace(
    tools=[],
    mcp_servers={"records": {"callers": "code", "tools": {"get": {"callers": "both"}}}},
    plugins=[],
)


@pytest.fixture(autouse=True)
def _fresh_conf(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("CLIO_TOOLS_CALLERS", raising=False)
    monkeypatch.delenv("CLIO_TOOLS_PERMISSIONS", raising=False)
    conf.reload()
    yield
    conf.reload()


class TestCallers:
    def test_code_tools_leave_the_listing_and_are_marked_in_the_index(self) -> None:
        program = _program()
        apply_disclosure(program, _RESOLUTION)
        assert program._clio_code_only == {"records_list"}
        assert "records_list" in program._clio_unlisted
        assert "records_list" not in program._clio_deferred_catalog
        table = stub_table(
            program.tools,
            ("records",),
            direct_only=program._clio_direct_only,
            code_only=program._clio_code_only,
        )
        assert set(table["groups"]["records"]) == {"list", "get"}
        assert "ask_user" not in json.dumps(table)
        index = index_text(table)
        assert "Only in scripts (not in your tool list): tools.records.list" in index
        assert "Tools in scripts:" in program.tools["run_code"].desc
        assert "tools.records.list" in program.tools["run_code"].desc

    def test_a_direct_call_to_a_code_tool_is_refused(self) -> None:
        program = _program()
        apply_disclosure(program, _RESOLUTION)
        assert "only be called from a run_code script" in settings.call_refusal(
            program, "records_list"
        )
        assert settings.call_refusal(program, "records_get") == ""

    def test_without_run_code_code_tools_fall_back_to_direct(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        program = _program(run_code=False)
        with caplog.at_level(logging.INFO):
            apply_disclosure(program, _RESOLUTION)
        assert program._clio_code_only == frozenset()
        assert program._clio_callers_fallback == settings.TOOL_CALLERS_CODE_UNAVAILABLE
        assert "records_list" not in program._clio_unlisted
        assert settings.call_refusal(program, "records_list") == ""
        assert settings.TOOL_CALLERS_CODE_UNAVAILABLE in caplog.text

    def test_configuration_overrides_the_declarations(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv(
            "CLIO_TOOLS_CALLERS", json.dumps({"records_list": "both", "records_get": "nobody"})
        )
        conf.reload()
        with caplog.at_level(logging.WARNING):
            assert configured_callers() == {"records_list": "both"}
        assert "tool_settings_value_invalid" in caplog.text
        program = _program()
        apply_disclosure(program, _RESOLUTION)
        assert program._clio_code_only == frozenset()
        found = settings.declared_callers(
            "records_list", program.tools["records_list"], _RESOLUTION
        )
        assert found == settings.Declared("both", "configuration")
        assert settings.declared_callers(
            "ask_user", program.tools["ask_user"], _RESOLUTION
        ) == settings.Declared("direct", "built-in")

    def test_a_denied_tool_is_hidden_and_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLIO_TOOLS_PERMISSIONS", json.dumps({"records_get": "deny"}))
        conf.reload()
        assert configured_permissions() == {"records_get": "deny"}
        program = _program()
        apply_disclosure(program, _RESOLUTION)
        assert "records_get" not in program.tools
        assert program._clio_blocked == {"records_get": settings.TOOL_PERMISSION_DENY}
        assert settings.TOOL_PERMISSION_DENY in settings.call_refusal(program, "records_get")
        assert "records_get" not in program.tools["run_code"].desc

    def test_a_blocked_tools_flags_stay_known_to_the_tools_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from clio_agent.gact.agents.tool_instrumentation import READ_ONLY_ATTR
        from clio_agent.gact.runtime.app_state import per_app_dict

        monkeypatch.setenv("CLIO_TOOLS_PERMISSIONS", json.dumps({"records_get": "deny"}))
        conf.reload()
        program = _program()
        setattr(program.tools["records_get"].func, READ_ONLY_ATTR, True)
        app = SimpleNamespace(state=SimpleNamespace())
        token = _ctx.set_app(app)
        try:
            apply_disclosure(program, _RESOLUTION)
        finally:
            _ctx.reset(token)
        assert "records_get" not in program.tools
        assert per_app_dict("tool_facts", app=app)["records_get"]["read_only"] is True


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    with tool_workspace_context(str(tmp_path)):
        yield tmp_path
    kernels.close_session("ts")


@pytest.mark.timeout(120)  # starts a real kernel process
class TestScripts:
    def _agent(self) -> Any:
        program = _program()
        apply_disclosure(program, _RESOLUTION)
        object.__setattr__(program, "_clio_tool_servers", frozenset({"records"}))
        return program

    def test_a_code_tool_is_callable_from_a_script(self, workspace: Path) -> None:
        outcome = tool_module.run_script(
            self._agent(), "(await tools.records.list(page=2))['ids']", session_id="ts", scope="m"
        )
        assert outcome["status"] == "completed", outcome
        assert outcome["value"] == "['r2']"

    def test_a_direct_tool_is_refused_from_a_script(self, workspace: Path) -> None:
        code = (
            "try:\n    await tools.call('ask_user', question='x')\n"
            "except ToolError as exc:\n    print(exc)"
        )
        outcome = tool_module.run_script(self._agent(), code, session_id="ts", scope="m")
        assert "ask_user cannot be called from a script" in outcome["output"]


@pytest.mark.parametrize(
    ("rule", "default", "mode", "expected"),
    [
        ("", "", "ask", ("", "mode")),
        ("allow", "deny", "bypass", ("deny", "default")),  # deny from any layer wins
        ("deny", "allow", "ask", ("deny", "rule")),
        ("allow_session", "ask", "ask", ("allow", "rule")),  # the person's rule beats the default
        ("ask", "allow", "bypass", ("ask", "rule")),  # a person's ask survives bypass
        ("", "allow", "ask", ("allow", "default")),
        ("", "ask", "auto-edits", ("ask", "default")),  # the default beats the mode
        ("", "ask", "bypass", ("", "mode")),  # bypass skips the default's question
    ],
)
def test_the_layers_order(rule: str, default: str, mode: str, expected: tuple[str, str]) -> None:
    layered = layers.decide(rule, default, approval_mode=mode)
    assert (layered.action, layered.source) == expected


# --------------------------------------------------------------------- the real gate

#: A non-read call that no catalog tag classifies; ``fs_write_file`` is a catalog write
#: (auto-edits allows it); ``fs_read_file`` is read-only.
_NON_READ = "shell.exec"


def _session(client: Any, mode: str) -> str:
    return client.post("/v1/sessions", json={"title": "t", "approval_mode": mode}).json()["id"]


def _pending(app: Any) -> dict[str, Any]:
    deadline = time.time() + TURN_SIGNAL_BACKSTOP_S
    while time.time() < deadline:
        rows = [r for r in app.state.permissions.values() if r["status"] == "pending"]
        if rows:
            return rows[0]
        time.sleep(0.02)
    raise AssertionError("no permission question appeared")


def _defaults(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    monkeypatch.setenv("CLIO_TOOLS_PERMISSIONS", json.dumps(values))
    conf.reload()


def _decide(app: Any, sid: str, name: str, args: dict[str, Any], grant: Any = None) -> str:
    from clio_agent.gact.app import _make_permission_gate, _tool_session_context

    gate = _make_permission_gate(app)
    token = grants.bind_grant(grant) if grant is not None else None
    try:
        with _tool_session_context(sid):
            return gate(name, args)
    finally:
        if token is not None:
            grants.reset_grant(token)


def _reasons(app: Any) -> list[str]:
    return [str(r.get("reason") or "") for r in app.state.permissions.values()]


@pytest.mark.usefixtures("host_agent_executor")
class TestGate:
    @pytest.mark.parametrize("mode", ["ask", "auto-edits", "bypass"])
    def test_a_default_deny_blocks_reads_and_writes_in_every_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        from fastapi.testclient import TestClient

        from clio_agent.gact.app import build_app

        _defaults(monkeypatch, {"fs_read_file": "deny", _NON_READ: "deny"})
        app = build_app(sessions_path=tmp_path / "s.json")
        with TestClient(app) as client:
            sid = _session(client, mode)
            # A person's allow does not lift a deny from another layer.
            app.state.permission_policies = [
                {"scope": "workspace", "tool_name_pattern": _NON_READ, "action": "allow"}
            ]
            assert _decide(app, sid, "fs_read_file", {}) == "deny"
            assert _decide(app, sid, _NON_READ, {"cmd": "ls"}) == "deny"
            assert _reasons(app).count(layers.REASON_DEFAULT_DENY) == 2

    def test_a_default_allow_runs_in_ask_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        from clio_agent.gact.app import build_app

        _defaults(monkeypatch, {_NON_READ: "allow"})
        app = build_app(sessions_path=tmp_path / "s.json")
        with TestClient(app) as client:
            sid = _session(client, "ask")
            assert _decide(app, sid, _NON_READ, {"cmd": "ls"}) == "allow"
            assert _reasons(app) == [layers.REASON_DEFAULT_ALLOW]

    def test_a_default_ask_overrides_auto_edits_but_not_bypass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        from clio_agent.gact.app import build_app

        app = build_app(sessions_path=tmp_path / "s.json")
        with TestClient(app) as client:
            edits, bypass = _session(client, "auto-edits"), _session(client, "bypass")
            assert _decide(app, edits, "fs_write_file", {}) == "allow"  # no default: the mode
            _defaults(monkeypatch, {"fs_write_file": "ask"})
            assert _decide(app, bypass, "fs_write_file", {}) == "allow"
            result: dict[str, str] = {}
            thread = threading.Thread(
                target=lambda: result.update(d=_decide(app, edits, "fs_write_file", {}))
            )
            thread.start()
            row = _pending(app)
            client.post(f"/v1/permissions/{row['id']}", json={"action": "deny"})
            thread.join(timeout=5)
            assert result["d"] == "deny"

    def test_the_persons_rule_overrides_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        from clio_agent.gact.app import build_app

        _defaults(monkeypatch, {_NON_READ: "ask"})
        app = build_app(sessions_path=tmp_path / "s.json")
        with TestClient(app) as client:
            sid = _session(client, "ask")
            app.state.permission_policies = [
                {"scope": "workspace", "tool_name_pattern": _NON_READ, "action": "allow"}
            ]
            assert _decide(app, sid, _NON_READ, {"cmd": "ls"}) == "allow"
            assert _reasons(app) == ["policy_allow"]

    def test_a_persons_deny_rule_blocks_a_read(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        from clio_agent.gact.app import build_app

        app = build_app(sessions_path=tmp_path / "s.json")
        with TestClient(app) as client:
            sid = _session(client, "bypass")
            assert _decide(app, sid, "fs_read_file", {}) == "allow"
            app.state.permission_policies = [
                {"scope": "workspace", "tool_name_pattern": "fs_read_file", "action": "deny"}
            ]
            assert _decide(app, sid, "fs_read_file", {}) == "deny"

    def test_nested_script_calls_obey_the_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        from clio_agent.gact.app import build_app

        app = build_app(sessions_path=tmp_path / "s.json")
        with TestClient(app) as client:
            sid = _session(client, "auto-edits")
            grant = grants.ScriptGrant("call_1", "await tools.shell.exec()")
            # No default: the script's approval covers the nested call in auto-edits.
            assert _decide(app, sid, _NON_READ, {"cmd": "a"}, grant) == "allow"
            assert _reasons(app)[-1] == grants.REASON_SCRIPT_GRANT
            _defaults(monkeypatch, {_NON_READ: "deny"})
            assert _decide(app, sid, _NON_READ, {"cmd": "b"}, grant) == "deny"
            _defaults(monkeypatch, {_NON_READ: "ask"})
            result: dict[str, str] = {}

            def fire() -> None:
                result["d"] = _decide(app, sid, _NON_READ, {"cmd": "c"}, grant)

            thread = threading.Thread(target=fire)
            thread.start()
            row = _pending(app)  # the default's ask is not covered by the script's approval
            assert row["script"]["call_id"] == "call_1"
            client.post(f"/v1/permissions/{row['id']}", json={"action": "allow"})
            thread.join(timeout=5)
            assert result["d"] == "allow"

    def test_native_tools_follow_rules_and_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        from clio_agent.gact.app import build_app

        tool = ClioNativeTool(lambda: "done", name="write_notes", desc="Write notes.")
        app = build_app(sessions_path=tmp_path / "s.json")
        with TestClient(app) as client:
            sid = _session(client, "bypass")
            app_token, sid_token = _ctx.set_app(app), _ctx.set_session_id(sid)
            try:
                with layers.native_gate(tool, "write_notes", {}) as refused:
                    assert refused == ""  # nothing set: CLIO's own tools run as before
                _defaults(monkeypatch, {"write_notes": "deny"})
                with layers.native_gate(tool, "write_notes", {}) as refused:
                    assert "blocks it" in refused
                assert _reasons(app)[-1] == layers.REASON_DEFAULT_DENY
                _defaults(monkeypatch, {})
                app.state.permission_policies = [
                    {"scope": "workspace", "tool_name_pattern": "write_notes", "action": "ask"}
                ]
                result: dict[str, Any] = {}

                def fire() -> None:
                    inner = _ctx.set_app(app), _ctx.set_session_id(sid)
                    try:
                        with layers.native_gate(tool, "write_notes", {}) as refused:
                            result["refused"] = refused
                            result["answered"] = layers.answered("write_notes")
                    finally:
                        _ctx.reset(inner[1])
                        _ctx.reset(inner[0])

                thread = threading.Thread(target=fire)
                thread.start()
                row = _pending(app)  # a person's ask asks even in bypass
                client.post(f"/v1/permissions/{row['id']}", json={"action": "allow"})
                thread.join(timeout=5)
                assert result == {"refused": "", "answered": True}
            finally:
                _ctx.reset(sid_token)
                _ctx.reset(app_token)


# --------------------------------------------------------------------- the tools page


@pytest.mark.usefixtures("host_agent_executor")
def test_the_tools_page_reads_and_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from clio_agent.gact.app import build_app
    from tests._config_layer import read_config

    monkeypatch.chdir(tmp_path)
    conf.reload()
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        view = client.get("/v1/settings/tools", params={"ref": "clio-builtin/core"}).json()
        rows = {row["name"]: row for row in view["tools"]}
        assert set(view["approval_modes"]) >= {"ask", "auto-edits", "bypass"}
        assert rows["write_todos"]["group"] == "write"
        assert rows["write_todos"]["callers"] == "both"  # unknown until an agent is built
        assert client.get("/v1/settings/tools", params={"ref": "nope/nope"}).status_code == 404

        saved = client.put(
            "/v1/settings/tools",
            json={"tools": ["query_tasks", "cron_list"], "permission": "deny", "callers": "code"},
        )
        assert saved.status_code == 200, saved.text
        everywhere = [
            p for p in app.state.permission_policies if p.get("tool_name_pattern") == "cron_list"
        ]
        assert [(p["scope"], p.get("scope_id", ""), p["action"]) for p in everywhere] == [
            ("workspace", "", "deny")
        ]
        assert read_config()["tools"]["callers"] == {"query_tasks": "code", "cron_list": "code"}
        assert configured_callers()["cron_list"] == "code"
        rows = {
            r["name"]: r
            for r in client.get("/v1/settings/tools", params={"ref": "clio-builtin/core"}).json()[
                "tools"
            ]
        }
        assert rows["cron_list"]["permission"] == "deny"
        assert rows["cron_list"]["outcomes"]["bypass"]["direct"] == "blocked"
        assert rows["cron_list"]["rules"][0]["where"] == "everywhere"

        cleared = client.put("/v1/settings/tools", json={"tools": ["cron_list"], "callers": None})
        assert cleared.status_code == 200
        assert "cron_list" not in read_config()["tools"]["callers"]
        removed = client.post(
            "/v1/settings/tools/rules/remove",
            json={"tool": "cron_list", "scope": "workspace", "scope_id": "", "action": "deny"},
        )
        assert removed.json() == {"removed": 1}
        bad = client.put("/v1/settings/tools", json={"tools": ["cron_list"], "permission": "never"})
        assert bad.status_code == 422


@pytest.mark.usefixtures("host_agent_executor")
def test_the_tools_page_reads_one_mcp_server_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    from mcp.types import Tool, ToolAnnotations

    from clio_agent.gact import tool_settings_page as page
    from clio_agent.gact.app import build_app

    monkeypatch.chdir(tmp_path)
    conf.reload()
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        pending = client.get("/v1/settings/tools", params={"server": "docs"}).json()
        assert pending["tools"] == []
        assert pending["servers_not_loaded"] == ["docs"]

        # The executor holds listed ``mcp.types.Tool`` models, whose annotations are read
        # as attributes, so the fixture uses the same shape.
        definitions = {
            "docs_search": Tool(
                name="docs_search",
                description="Search the docs.",
                annotations=ToolAnnotations(readOnlyHint=True),
                inputSchema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ),
            "docs_delete": Tool(name="docs_delete", description="Delete a page.", inputSchema={}),
            "other_tool": Tool(name="other_tool", description="Not this server's.", inputSchema={}),
        }
        monkeypatch.setattr(page, "_mcp_definitions", lambda _app: definitions)
        view = client.get("/v1/settings/tools", params={"server": "docs"}).json()
        rows = {row["name"]: row for row in view["tools"]}
        assert view["plugin"] == "docs"
        assert view["servers_not_loaded"] == []
        assert set(rows) == {"docs_search", "docs_delete"}
        assert rows["docs_search"]["server"] == "docs"
        assert rows["docs_search"]["group"] == "read"
        assert rows["docs_delete"]["group"] == "write"
        assert rows["docs_search"]["plugin_permission_source"] == "annotations"
        assert rows["docs_search"]["input_schema"]["required"] == ["query"]
        assert rows["docs_delete"]["input_schema"] == {}
        assert set(rows["docs_delete"]["outcomes"]) == set(view["approval_modes"])

        assert client.get("/v1/settings/tools").status_code == 422
        both = client.get("/v1/settings/tools", params={"ref": "clio-builtin/core", "server": "x"})
        assert both.status_code == 422
        assert client.get("/v1/settings/tools", params={"server": " "}).status_code == 422


def test_mcp_definitions_include_workspace_fleets() -> None:
    import threading
    from types import SimpleNamespace

    from clio_agent.gact import tool_settings_page as page

    def executor(definitions: dict) -> SimpleNamespace:
        return SimpleNamespace(get_all_tool_definitions=lambda: definitions)

    workspaces = {"D:/ws": executor({"docs_search": {"name": "docs_search"}})}
    agent = SimpleNamespace(
        tool_executor=executor({"fs_read": {"name": "fs_read"}}),
        _workspace_state=lambda: (threading.Lock(), workspaces, {}),
    )
    definitions = page._mcp_definitions(SimpleNamespace(state=SimpleNamespace(agent=agent)))
    assert set(definitions) == {"fs_read", "docs_search"}
    assert page._mcp_definitions(SimpleNamespace(state=SimpleNamespace(agent=None))) == {}
