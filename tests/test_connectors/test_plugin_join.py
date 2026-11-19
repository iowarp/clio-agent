"""A plugin the person has set up joins new conversations by default, and its servers'
recorded instructions find it while it is not in one."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import offer_list, plugin_join, plugin_offer
from clio_agent.gact.agents import resolution as agent_resolution
from clio_agent.gact.agents.resolution import AGENT_DELTA_KEY, SessionAgentUnavailable
from clio_agent.plugins import recorded_instructions
from clio_agent.runtime import trace
from tests.test_connectors.test_plugin_offer import RECORDS, _record, app  # noqa: F401

SETUP = {"setup": {"files": ["config/sites.yaml"]}}


@pytest.fixture
def library(app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:  # noqa: F811
    """``remote`` (set up), ``idle`` (declares setup, has none) join candidates."""

    store = tmp_path / "store"
    monkeypatch.setattr("clio_agent.plugins.service.plugin_store_root", lambda: store)
    recorded_instructions.forget_loaded()
    for rid, text in (("remote", "- id: lab\n  kind: ssh\n"), ("idle", "[]\n")):
        folder = store / "data" / rid / "config"
        folder.mkdir(parents=True)
        (folder / "sites.yaml").write_text(text, encoding="utf-8")
    added = [_record("remote", host=SETUP), _record("idle", host=SETUP)]
    RECORDS.extend(added)
    calls: list[dict[str, Any]] = []

    def prepare(_app: Any, metadata: dict[str, Any], requested: str) -> Any:
        calls.append(dict(metadata[AGENT_DELTA_KEY]))
        if app.state.refuse:
            raise SessionAgentUnavailable(requested, "plugin_unresolvable", [])
        refs = metadata[AGENT_DELTA_KEY]["add"]
        return SimpleNamespace(plugins=[{"id": ref.split("/")[-1]} for ref in refs])

    monkeypatch.setattr(agent_resolution, "prepare_session_agent", prepare)
    app.state.refuse = False
    app.state.calls = calls
    yield app
    for record in added:
        RECORDS.remove(record)
    recorded_instructions.forget_loaded()


def _join(the_app: Any, metadata: dict[str, Any] | None = None) -> tuple[dict[str, Any], Any]:
    metadata = metadata if metadata is not None else {}
    before = SimpleNamespace(plugins=[{"id": "already-here"}])
    after = plugin_join.join_set_up_plugins(the_app, metadata, "clio/standard", before)
    return metadata, after


def test_a_set_up_plugin_joins_a_new_conversation(library: Any) -> None:
    events: list[str] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(trace, "event", lambda _c, fmt, *a: events.append(fmt % a))
        metadata, after = _join(library)

    assert metadata[AGENT_DELTA_KEY] == {"add": ["lab/remote"], "remove": []}
    assert [row["id"] for row in after.plugins] == ["remote"]
    assert events == ["plugin_joined_set_up plugins=lab/remote"]


def test_the_persons_choices_keep_it_out(library: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    removed = {AGENT_DELTA_KEY: {"add": [], "remove": ["lab/remote"]}}
    assert _join(library, removed)[0] == removed
    # Already in the request's delta: not added twice.
    asked = {AGENT_DELTA_KEY: {"add": ["lab/remote"], "remove": []}}
    assert _join(library, asked)[0][AGENT_DELTA_KEY] == {"add": ["lab/remote"], "remove": []}
    offer_list._store(offer_list.Overlay(exclude=frozenset({"plugin:lab/remote"})))
    assert _join(library)[0] == {}
    offer_list._store(offer_list.Overlay())
    monkeypatch.setenv("CLIO_PLUGINS_JOIN_SET_UP", "false")
    assert _join(library)[0] == {}
    assert library.state.calls == []


def test_an_agent_that_already_has_it_or_cannot_take_it(library: Any) -> None:
    here = SimpleNamespace(plugins=[{"id": "remote"}])
    metadata: dict[str, Any] = {"keep": 1}
    assert plugin_join.join_set_up_plugins(library, metadata, "a", here) is here
    assert metadata == {"keep": 1}

    library.state.refuse = True
    events: list[str] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(trace, "event", lambda _c, fmt, *a: events.append(fmt % a))
        metadata, after = _join(library, {"keep": 1})
    assert metadata == {"keep": 1}
    assert [row["id"] for row in after.plugins] == ["already-here"]
    assert events == ["plugin_join_set_up_unresolved plugins=lab/remote (plugin_unresolvable)"]


def test_recorded_instructions_find_a_plugin_not_in_the_conversation(library: Any) -> None:
    recorded_instructions.remember("remote", "remote", "Compute sites configured: lab (ssh).")

    rows = {row["id"]: row for row in plugin_offer.offerable(library, "sess_a")}
    assert rows["remote"]["instructions"] == "Compute sites configured: lab (ssh)."
    found = plugin_offer.offerable_matching(library, "sess_a", "lab ssh")
    assert [row["id"] for row in found] == ["remote"]
    assert plugin_offer.offerable_matching(library, "sess_a", "lab zebra") == []
