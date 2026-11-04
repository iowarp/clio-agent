"""Progressive tool disclosure: the catalog, the deferred split and ``search_tools``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import dspy
import pytest

from clio_agent.gact.agents.deferred_tools import (
    SEARCH_TOOLS,
    apply_disclosure,
    plan_disclosure,
    sync_deferred_catalog,
)
from clio_agent.gact.search_tools import (
    LOADED_TOOLS_KEY,
    _present,
    _record_loaded,
    listed_loaded_tools,
    promote_loaded_tools,
    search_result,
)
from clio_agent.gact.tool_catalog import CatalogEntry, ToolCatalog
from clio_agent.lm.engines.text_tools import render_system


def _tool(name: str, desc: str = "", **args: str) -> dspy.Tool:
    def body(**kwargs: Any) -> str:
        return "ok"

    return dspy.Tool(
        body,
        name=name,
        desc=desc or f"The {name} tool.",
        args={a: {"type": t} for a, t in args.items()},
    )


def _catalog() -> ToolCatalog:
    return ToolCatalog(
        [
            CatalogEntry("home_light_set", "home", "Turn a light on or off, or dim it."),
            CatalogEntry("home_light_list", "home", "List the lights in the house."),
            CatalogEntry("home_lock_set", "home", "Lock or unlock a door."),
            CatalogEntry("calendar_event_create", "calendar", "Create a calendar event."),
            CatalogEntry(
                "inventory_count",
                "inventory",
                "Count items on hand.",
                {"type": "object", "properties": {"sku": {"type": "string"}}},
            ),
        ]
    )


class TestCatalog:
    def test_select_returns_exact_names_and_missing(self) -> None:
        found, missing = _catalog().search("select:home_lock_set, nope ,inventory_count", 1)
        assert [e.name for e in found] == ["home_lock_set", "inventory_count"]
        assert missing == ["nope"]

    def test_keywords_rank_by_bm25(self) -> None:
        found, _ = _catalog().search("dim the light")
        assert found[0].name == "home_light_set"
        assert "calendar_event_create" not in [e.name for e in found]

    def test_exact_name_in_keywords_comes_first(self) -> None:
        found, _ = _catalog().search("lights inventory_count")
        assert found[0].name == "inventory_count"

    def test_required_term_filters_names(self) -> None:
        found, _ = _catalog().search("+lock door")
        assert [e.name for e in found] == ["home_lock_set"]

    def test_argument_names_are_searchable(self) -> None:
        found, _ = _catalog().search("sku")
        assert [e.name for e in found] == ["inventory_count"]

    def test_by_owner_is_sorted(self) -> None:
        groups = _catalog().by_owner()
        assert list(groups) == ["calendar", "home", "inventory"]
        assert [e.name for e in groups["home"]] == [
            "home_light_list",
            "home_light_set",
            "home_lock_set",
        ]


def _resolution(**servers: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        mcp_servers=servers,
        tools=[
            {"name": "write_todos", "plugin": "core", "defer": True},
            {"name": "cron_list", "plugin": "core", "defer": True},
            {"name": "ask_user", "plugin": "core"},
        ],
        plugins=[{"id": "core", "description": "Conversation runtime."}],
    )


def _tools(*names: str) -> dict[str, dspy.Tool]:
    return {name: _tool(name) for name in names}


class TestPlan:
    def test_declared_plugin_tools_defer_except_the_core_loop(self) -> None:
        tools = _tools(SEARCH_TOOLS, "write_todos", "cron_list", "ask_user")
        plan = plan_disclosure(_resolution(), tools)
        assert plan.deferred == {"cron_list"}
        assert "- core: Conversation runtime." in plan.entry
        assert "cron_list" in plan.entry

    def test_undeclared_server_defers_over_the_size_threshold(self) -> None:
        names = [f"home_tool_{i}" for i in range(40)]
        tools = _tools(SEARCH_TOOLS, *names, "web_fetch")
        resolution = _resolution(home={"description": "Smart home."}, web={})
        plan = plan_disclosure(resolution, tools, context_tokens=10_000)
        assert plan.deferred_servers == {"home"}
        assert set(names) <= plan.deferred and "web_fetch" not in plan.deferred
        assert "- home: Smart home." in plan.entry

    def test_a_server_without_a_description_is_named_by_its_plugin(self) -> None:
        tools = _tools(SEARCH_TOOLS, "home_light_set")
        plan = plan_disclosure(_resolution(home={"defer": True, "plugin": "core"}), tools)
        assert "- home: Conversation runtime." in plan.entry

    def test_server_declaration_wins_over_size(self) -> None:
        tools = _tools(SEARCH_TOOLS, "web_fetch", "big_one")
        resolution = _resolution(web={"defer": True}, big={"defer": False})
        plan = plan_disclosure(resolution, tools, context_tokens=100)
        assert plan.deferred == {"web_fetch"}

    def test_fs_and_shell_are_never_deferred(self) -> None:
        tools = _tools(SEARCH_TOOLS, "fs_read_file", "shell_bash")
        plan = plan_disclosure(_resolution(fs={"defer": True}, shell={"defer": True}), tools)
        assert plan.deferred == frozenset()

    def test_the_memory_tools_the_memory_injection_names_stay_listed(self) -> None:
        """The memory injection names memory_search and memory_read whenever memory is
        on; deferred, the model called memory_read blind (X2). SABOTAGE: drop them from
        the default never-defer list and both are deferred."""
        tools = _tools(SEARCH_TOOLS, "memory_search", "memory_read", "memory_write")
        plan = plan_disclosure(_resolution(memory={"defer": True}), tools)
        assert plan.deferred == {"memory_write"}

    def test_the_never_deferred_names_and_prefixes_are_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLIO_TOOLS_NEVER_DEFER", "cron_*, fs_read_file")
        tools = _tools(SEARCH_TOOLS, "fs_read_file", "shell_bash", "cron_list")
        resolution = _resolution(fs={"defer": True}, shell={"defer": True})
        plan = plan_disclosure(resolution, tools)
        assert plan.deferred == {"shell_bash"}

    def test_a_builtin_that_declares_nothing_is_deferred_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Built-ins follow MCP servers: deferred unless the core loop or declared
        listed. SABOTAGE: drop the ``declared is None and by_default`` branch and
        connected_data_connect stays listed."""
        resolution = SimpleNamespace(
            mcp_servers={},
            tools=[
                {"name": "connected_data_connect", "plugin": "core"},
                {"name": "create_artifact", "plugin": "core", "defer": False},
                {"name": "run_code", "plugin": "core"},
            ],
            plugins=[{"id": "core", "description": "Conversation runtime."}],
        )
        tools = _tools(SEARCH_TOOLS, "connected_data_connect", "create_artifact", "run_code")
        plan = plan_disclosure(resolution, tools)
        assert plan.deferred == {"connected_data_connect"}
        assert "connected_data_connect" in plan.entry
        monkeypatch.setenv("CLIO_TOOLS_DEFER_BUILTINS", "false")
        assert plan_disclosure(resolution, tools).deferred == frozenset()

    def test_search_tools_is_never_deferred_whatever_the_setting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLIO_TOOLS_NEVER_DEFER", "fs_*")
        tools = _tools(SEARCH_TOOLS, "ask_user")
        assert plan_disclosure(_resolution(), tools).deferred == {"ask_user"}

    def test_loaded_tools_stay_listed(self) -> None:
        tools = _tools(SEARCH_TOOLS, "cron_list")
        plan = plan_disclosure(_resolution(), tools, loaded=frozenset({"cron_list"}))
        assert plan.deferred == frozenset() and plan.entry == ""

    def test_without_search_tools_nothing_is_deferred(self) -> None:
        plan = plan_disclosure(_resolution(), _tools("cron_list"))
        assert plan.deferred == frozenset() and plan.entry == ""


class TestApply:
    def test_deferred_names_join_unlisted(self) -> None:
        program = SimpleNamespace(tools=_tools(SEARCH_TOOLS, "cron_list", "ask_user"))
        program._clio_unlisted = frozenset({"added_by_note"})
        entry = apply_disclosure(program, _resolution())
        assert program._clio_unlisted == {"added_by_note", "cron_list"}
        assert "cron_list" in program._clio_deferred_catalog
        assert entry.startswith("## Tools you load when needed")

    def test_nothing_deferred_unlists_search_tools(self) -> None:
        program = SimpleNamespace(tools=_tools(SEARCH_TOOLS, "ask_user"))
        assert apply_disclosure(program, _resolution()) == ""
        assert program._clio_unlisted == {SEARCH_TOOLS}

    def test_refresh_adds_a_deferred_servers_new_tool(self) -> None:
        names = [f"home_tool_{i}" for i in range(40)]
        program = SimpleNamespace(tools=_tools(SEARCH_TOOLS, *names))
        apply_disclosure(program, _resolution(home={}), context_tokens=10_000)
        program.tools["home_new"] = _tool("home_new")
        del program.tools["home_tool_0"]
        sync_deferred_catalog(program)
        assert "home_new" in program._clio_deferred_catalog
        assert "home_tool_0" not in program._clio_deferred_catalog
        assert "home_new" in program._clio_unlisted


class TestSearchResult:
    def test_loaded_schemas_reach_the_model(self) -> None:
        text, structured = search_result(_catalog(), "select:inventory_count")
        assert "## inventory_count  (inventory)" in text
        assert '"sku":{"type":"string"}' in text
        assert structured["tools"][0]["signature"] == "inventory_count(sku?: string)"

    def test_a_miss_names_the_deferred_tools(self) -> None:
        text, structured = search_result(_catalog(), "weather radar")
        assert structured["tools"] == []
        assert "home_lock_set" in text and "select:name" in text

    def test_the_search_bounds_are_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLIO_TOOLS_SEARCH_LIMIT", "1")
        monkeypatch.setenv("CLIO_TOOLS_SEARCH_MAX", "2")
        monkeypatch.setenv("CLIO_TOOLS_SEARCH_MISS_NAMES", "1")
        _, default = search_result(_catalog(), "light lock calendar inventory")
        assert len(default["tools"]) == 1
        _, capped = search_result(_catalog(), "light lock calendar inventory", 10)
        assert len(capped["tools"]) == 2
        text, _ = search_result(_catalog(), "weather radar")
        assert "calendar_event_create and 4 more" in text

    def test_no_catalog(self) -> None:
        text, structured = search_result(None, "anything")
        assert structured["deferred_total"] == 0
        assert "already in its tool list" in text

    def test_presentation(self) -> None:
        _, structured = search_result(_catalog(), "select:home_lock_set,missing_one")
        view = _present({"query": "x"}, "", structured)
        assert view["summary"] == "Loaded 1 tool"
        assert any(b.get("severity") == "warning" for b in view["blocks"])
        _, empty = search_result(_catalog(), "weather")
        assert _present({}, "", empty)["status"] == "degraded"


class _Sessions:
    def __init__(self) -> None:
        self.rows: dict[str, Any] = {"s": SimpleNamespace(metadata={})}

    def get(self, sid: str) -> Any:
        return self.rows.get(sid)

    def update(self, sid: str, *, metadata_patch: dict[str, Any]) -> None:
        self.rows[sid].metadata = {**self.rows[sid].metadata, **metadata_patch}


def test_loaded_tools_are_listed_after_the_prompt_is_rebuilt() -> None:
    app = SimpleNamespace(state=SimpleNamespace(sessions=_Sessions()))
    _record_loaded(app, "s", ["cron_list"])
    assert listed_loaded_tools(app, "s") == frozenset()
    promote_loaded_tools(app, "s")
    assert listed_loaded_tools(app, "s") == {"cron_list"}
    _record_loaded(app, "s", ["cron_list"])
    assert app.state.sessions.get("s").metadata[LOADED_TOOLS_KEY]["pending"] == []


@pytest.mark.parametrize("with_search", [True, False])
def test_tool_rules_mention_loaded_tools_only_with_search(with_search: bool) -> None:
    from clio_agent.gact.agents.clio_react import _function_tool

    names = [SEARCH_TOOLS, "ask_user"] if with_search else ["ask_user"]
    text = render_system("prompt", [_function_tool(_tool(n)) for n in names])
    assert ("search_tools loads them" in text) is with_search


def test_a_deferred_servers_new_tools_are_named_until_the_prompt_is_rebuilt() -> None:
    """TD11: tools a deferred server adds mid-session reach the model through the
    plugin-change note, and stop being named once the rebuilt prompt names them.
    SABOTAGE: drop ``tools_line`` from ``plugin_change_note`` and the name is missing."""
    from clio_agent.gact.agents.prompt_basis import (
        note_deferred_tools_added,
        plugin_change_note,
        settle_basis,
    )

    app = SimpleNamespace(state=SimpleNamespace(sessions=_Sessions()))
    names = [f"home_tool_{i}" for i in range(40)]
    program = SimpleNamespace(tools=_tools(SEARCH_TOOLS, *names))
    apply_disclosure(program, _resolution(home={}), context_tokens=10_000)
    program.tools["home_new"] = _tool("home_new")
    added = sync_deferred_catalog(program)
    assert added == ["home_new"]
    note_deferred_tools_added(app, "s", added)

    note = plugin_change_note(app, "s")
    assert "home_new" in note and "search_tools" in note
    settle_basis(app, "s")
    assert plugin_change_note(app, "s") == ""
