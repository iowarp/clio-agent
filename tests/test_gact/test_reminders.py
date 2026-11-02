"""Reminders: every CLIO injection as a Markdown template a person can switch off or edit
(docs/design/sdk/reminders.md)."""

from __future__ import annotations

import os
import re
from importlib import resources
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent import paths
from clio_agent.gact import reminders
from clio_agent.prompts import PromptRegistry, parse_prompt_text

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")


def _shipped() -> list[Any]:
    folder = resources.files("clio_agent.prompt_packs.builtin") / "reminders"
    return [
        parse_prompt_text(path.read_text(encoding="utf-8"), scope="builtin", source_path=path.name)
        for path in sorted(folder.iterdir(), key=lambda item: item.name)
        if path.name.endswith(".md")
    ]


def _override(source: str) -> Path:
    return paths.user_config_dir() / "prompts" / f"clio.reminder.{source}--default.md"


def test_every_shipped_reminder_is_a_valid_declared_template() -> None:
    """Each file is a reminder whose body uses exactly the variables it declares, and the
    registry lists it. SABOTAGE: add ``{{nope}}`` to a shipped body and this fails."""
    shipped = _shipped()
    assert {row.id for row in shipped} >= {
        "clio.reminder.explore_nudge",
        "clio.reminder.plan_stale",
    }
    listed = {row.id for row in PromptRegistry().list()}
    for row in shipped:
        assert row.id.startswith(reminders.REMINDER_PREFIX)
        assert row.metadata.get("kind") == "reminder", row.id
        assert row.validation_errors == [], (row.id, row.validation_errors)
        assert row.metadata.get("trigger"), row.id
        declared = {name.strip() for name in str(row.metadata.get("variables", "")).split(",")}
        used = set(_PLACEHOLDER.findall(row.profiles["default"].text))
        assert used == declared - {""}, row.id
        assert row.id in listed


def test_render_fills_declared_variables_and_runs_nothing() -> None:
    text = reminders.render("plan_stale", {"path": "/w/plan.md", "steps": 7})
    assert text is not None
    assert "(/w/plan.md)" in text and "in 7 steps" in text and "{{" not in text


def test_a_source_without_a_template_cannot_render() -> None:
    """A reminder never silently loses its text."""
    with pytest.raises(LookupError, match="clio.reminder.no_such_source"):
        reminders.render("no_such_source", {})
    assert reminders.active("no_such_source")  # no template: nothing to switch off


def test_switching_off_keeps_the_wording_and_silences_it(caplog: Any) -> None:
    """``update`` writes the person's own copy; the text survives, the switch is off.
    SABOTAGE: make ``render`` ignore ``active`` and the ``None`` assertion fails."""
    saved = reminders.update("plan_stale", active=False, params={"steps": 40})
    assert not saved.active and saved.scope == "global" and saved.params["steps"] == "40"
    stored = _override("plan_stale").read_text(encoding="utf-8")
    assert "x_kind: reminder" in stored and "{{path}}" in stored
    assert reminders.render("plan_stale", {"path": "p", "steps": 1}) is None
    assert not reminders.active("plan_stale")
    assert reminders.param("plan_stale", "steps", 25, int) == 40
    reminders.update("plan_stale", active=True)
    assert reminders.render("plan_stale", {"path": "p", "steps": 1})


def test_an_edit_on_disk_is_seen_at_once() -> None:
    """The resolution cache is keyed by the override files' modification times."""
    reminders.update("plan_stale", active=True)
    path = _override("plan_stale")
    head, _, _ = path.read_text(encoding="utf-8").rpartition("---")
    path.write_text(f"{head}---\nPlan {{{{path}}}} is quiet.\n", encoding="utf-8")
    stamp = path.stat().st_mtime_ns + 1_000_000
    os.utime(path, ns=(stamp, stamp))
    assert reminders.render("plan_stale", {"path": "x", "steps": 1}) == "Plan x is quiet."


def test_an_override_with_an_undeclared_placeholder_falls_back_to_the_builtin() -> None:
    """A typo cannot reach the agent: the invalid copy is reported, the built-in is used."""
    reminders.update("plan_stale", active=True)
    path = _override("plan_stale")
    head, _, _ = path.read_text(encoding="utf-8").rpartition("---")
    path.write_text(f"{head}---\nPlan {{{{nope}}}}.\n", encoding="utf-8")
    text = reminders.render("plan_stale", {"path": "x", "steps": 3})
    assert text is not None and text.startswith("The plan (x)")


def test_a_required_reminder_cannot_be_switched_off() -> None:
    """A source that carries data the agent needs can be reworded, never dropped."""
    shipped = next(row for row in _shipped() if row.metadata.get("required"))
    source = shipped.id.removeprefix(reminders.REMINDER_PREFIX)
    reminders.update(source, active=False)
    assert reminders.active(source)
    assert reminders.reminder(source).required  # type: ignore[union-attr]


def test_the_recorder_drops_a_switched_off_source() -> None:
    """Every injection recorded through the loop passes the switch. SABOTAGE: remove the
    ``active`` filter in ``StepRecorder.injections`` and the note is written."""
    from clio_agent.gact.agents.clio_react_record import StepRecorder

    written: list[Any] = []
    recorder = SimpleNamespace(
        arc=SimpleNamespace(render_working_set=lambda session, scope: []),
        session="sess_r",
        scope="s",
        expert_id="main",
        _write=lambda kind, record, step, span: written.append(record["source"]),
    )
    reminders.update("plan_stale", active=False)
    StepRecorder.injections(recorder, [("plan_stale", "quiet"), ("todos", "- [ ] a")])
    assert written == ["todos"]


def test_the_prompt_preview_renders_a_reminder_with_sample_values() -> None:
    """The Prompts page previews a reminder with its own variables, not the system-prompt
    placeholders. SABOTAGE: drop ``declared`` from ``render_prompt_text`` and this fails."""
    rendered = PromptRegistry().render(
        "clio.reminder.plan_stale", context={"path": "plan.md", "steps": "3"}
    )
    assert rendered is not None and rendered.validation_errors == []
    assert "(plan.md)" in rendered.text and "in 3 steps" in rendered.text


def test_an_override_cannot_clear_required() -> None:
    """``x_required`` is read from the shipped template only. SABOTAGE: read it from the
    resolved (overridden) metadata and the emptied copy switches the reminder off."""
    shipped = next(row for row in _shipped() if row.metadata.get("required"))
    source = shipped.id.removeprefix(reminders.REMINDER_PREFIX)
    reminders.update(source, active=False)
    path = _override(source)
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^x_required:.*$", 'x_required: ""', text)
    path.write_text(text, encoding="utf-8")
    stamp = path.stat().st_mtime_ns + 1_000_000
    os.utime(path, ns=(stamp, stamp))
    assert 'x_required: ""' in path.read_text(encoding="utf-8")
    assert reminders.active(source)
