"""A person's reminder switch is a settings overlay; plugin injections are listed, switched
and cannot be required; the resolution follows a session's plugin set
(docs/design/sdk/reminders.md)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent import injections, paths
from clio_agent.injections import resolution
from clio_agent.injections.declarations import Injection
from clio_agent.prompts import PromptRegistry

_NOTE = """---
name: note
event: turn_start
title: A plugin note
x_required: the plugin wants it always on
---
Remember the station list.
"""


def _override(source: str) -> Path:
    return paths.user_config_dir() / "prompts" / f"clio.reminder.{source}--default.md"


def _ship(monkeypatch: pytest.MonkeyPatch, name: str, text: str) -> None:
    """A later release ships ``name`` with ``text``."""
    shipped = dict(resolution.core_injections())
    shipped[name] = dataclasses.replace(shipped[name], text=text)
    monkeypatch.setattr(resolution, "core_injections", lambda: shipped)
    resolution._CACHE.clear()


def test_a_switched_reminder_keeps_following_the_shipped_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching a reminder saved its whole text, so a later release's wording never
    reached that person. SABOTAGE: save ``resolved.text`` in ``update`` again and the new
    wording is not rendered."""
    injections.update("plan_stale", params={"steps": 40})
    stored = _override("plan_stale").read_text(encoding="utf-8")
    assert "x_overlay: true" in stored and "x_param_steps: 40" in stored
    assert stored.rstrip().endswith("---")  # no body
    _ship(monkeypatch, "plan_stale", "Newer wording: {{path}} quiet for {{steps}} steps.")
    assert injections.render("plan_stale", {"path": "p", "steps": 40}) == (
        "Newer wording: p quiet for 40 steps."
    )
    assert injections.param("plan_stale", "steps", 25, int) == 40


def test_saving_the_shipped_text_back_is_a_settings_overlay() -> None:
    """The Reminders tab sends the current text with the switch: unchanged shipped text is
    no text override. A setting equal to the shipped one is not stored, and with nothing
    left the person's copy is removed."""
    registry = PromptRegistry()
    shipped = registry.resolve("clio.reminder.explore_nudge")
    assert shipped is not None
    registry.save(shipped.id, text=shipped.text, metadata={**shipped.metadata, "active": False})
    stored = _override("explore_nudge").read_text(encoding="utf-8")
    assert "x_overlay: true" in stored and "x_active: false" in stored
    assert "x_trigger" not in stored and "x_param_after" not in stored
    assert not injections.active("explore_nudge")
    registry.save(shipped.id, text="", metadata={"active": True})
    assert not _override("explore_nudge").exists()
    assert injections.active("explore_nudge")


def test_an_edited_text_is_kept_when_switched(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = PromptRegistry()
    shipped = registry.resolve("clio.reminder.plan_stale")
    assert shipped is not None
    registry.save(shipped.id, text="Mine: {{path}}.", metadata=dict(shipped.metadata))
    saved = injections.update("plan_stale", active=False)
    assert not saved.active and saved.scope == "global"
    injections.update("plan_stale", active=True)
    assert injections.render("plan_stale", {"path": "p", "steps": 1}) == "Mine: p."


def test_the_api_accepts_a_switch_without_text(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from clio_agent.gact.app import build_app

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        url = "/v1/prompts/clio.reminder.todos"
        saved = client.put(url, json={"scope": "global", "metadata": {"active": False}})
        assert saved.status_code == 200, saved.text
        stored = Path(saved.json()["prompt"]["source_path"]).read_text(encoding="utf-8")
        assert "x_overlay: true" in stored and "x_active: false" in stored
        shipped = client.get(url).json()["prompt"]
        assert shipped["text"].strip() == resolution.core_injections()["todos"].text
        assert not injections.active("todos", app=app)
        assert client.put("/v1/prompts/core.custom", json={}).status_code == 422


def _plugin(monkeypatch: pytest.MonkeyPatch, key: str, rows: list[Injection]) -> None:
    from clio_agent.injections import plugin_set

    monkeypatch.setattr(plugin_set, "plugin_set_key", lambda app, sid: key)
    monkeypatch.setattr(plugin_set, "plugin_injections", lambda app, sid: rows)


def _row(text: str, **extra: Any) -> Injection:
    return Injection(name="p.note", event="turn_start", text=text, plugin="p", **extra)


def test_the_resolution_follows_the_sessions_plugin_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cache kept a removed plugin's injections and an updated one's old text.
    SABOTAGE: drop ``plugin_set_key`` from the cache key and the removal is not seen."""
    _plugin(monkeypatch, "d1", [_row("one")])
    assert injections.fire("turn_start", {}, session_id="sess_p") == [("p.note", "one")]
    _plugin(monkeypatch, "d2", [_row("two")])
    assert injections.fire("turn_start", {}, session_id="sess_p") == [("p.note", "two")]
    _plugin(monkeypatch, "d3", [])
    assert injections.fire("turn_start", {}, session_id="sess_p") == []
    assert injections.injection("p.note", session_id="sess_p") is None


def test_a_plugin_injection_is_switchable_and_never_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``update`` raised LookupError for a plugin's injection, and a plugin could mark its
    own text required. SABOTAGE: read ``row.required`` for plugins and it stays on."""
    _plugin(monkeypatch, "d1", [_row("one", required="always")])
    found = injections.injection("p.note", session_id="sess_p")
    assert found is not None and found.required == ""
    saved = injections.update("p.note", active=False, session_id="sess_p")
    assert not saved.active
    assert injections.fire("turn_start", {}, session_id="sess_p") == []
    injections.update("p.note", active=True, session_id="sess_p")
    assert injections.fire("turn_start", {}, session_id="sess_p") == [("p.note", "one")]


def test_installed_plugins_injections_are_listed_and_checked(tmp_path: Path) -> None:
    from clio_agent.injections.check import injection_diagnostics
    from clio_agent.injections.listing import with_plugin_reminders
    from clio_agent.plugins.records import PluginRecord

    folder = tmp_path / "ai.iowarp.clio" / "injections"
    folder.mkdir(parents=True)
    (folder / "note.md").write_text(_NOTE, encoding="utf-8")
    record = PluginRecord(
        id="stations", version="1", description="", kind="plugin", format="agent-plugins",
        root=str(tmp_path),
    )  # fmt: skip
    store = SimpleNamespace(records=lambda: [record])
    app = SimpleNamespace(state=SimpleNamespace(plugin_service=SimpleNamespace(store=store)))
    registry = with_plugin_reminders(PromptRegistry(), app)
    row = registry.get("clio.reminder.stations.note")
    assert row is not None and row.metadata["kind"] == "reminder"
    assert row.metadata["plugin"] == "stations" and "required" not in row.metadata
    registry.save(row.id, text="Remember the station list.", metadata={"active": False})
    assert "x_overlay: true" in _override("stations.note").read_text(encoding="utf-8")
    codes = {d.code for d in injection_diagnostics(record)}
    assert "injection_required_ignored" in codes
