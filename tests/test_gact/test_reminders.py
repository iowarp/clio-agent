"""Reminders: every CLIO injection as a Markdown template a person can switch off or edit
(docs/design/sdk/reminders.md)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent import injections, paths
from clio_agent.injections.declarations import core_dir
from clio_agent.injections.template import names_used
from clio_agent.prompts import PromptRegistry, parse_prompt_text


def _shipped() -> list[Any]:
    folder = core_dir()
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
        assert row.id.startswith(injections.PREFIX)
        assert row.metadata.get("kind") == "reminder", row.id
        assert row.validation_errors == [], (row.id, row.validation_errors)
        assert row.metadata.get("trigger"), row.id
        declared = {name.strip() for name in str(row.metadata.get("variables", "")).split(",")}
        used = set(names_used(row.profiles["default"].text, row.id))
        assert used == declared - {""}, row.id
        assert row.id in listed


def test_render_fills_declared_variables_and_runs_nothing() -> None:
    text = injections.render("plan_stale", {"path": "/w/plan.md", "steps": 7})
    assert text is not None
    assert "(/w/plan.md)" in text and "in 7 steps" in text and "{{" not in text


def test_a_source_without_a_template_cannot_render() -> None:
    """A reminder never silently loses its text."""
    with pytest.raises(LookupError, match="clio.reminder.no_such_source"):
        injections.render("no_such_source", {})
    assert injections.active("no_such_source")  # no template: nothing to switch off


def test_switching_off_keeps_the_wording_and_silences_it(caplog: Any) -> None:
    """``update`` writes the person's settings overlay: the switch and the setting, never
    the text. SABOTAGE: make ``render`` ignore ``active`` and the ``None`` assertion fails."""
    saved = injections.update("plan_stale", active=False, params={"steps": 40})
    assert not saved.active and saved.scope == "global" and saved.params["steps"] == "40"
    stored = _override("plan_stale").read_text(encoding="utf-8")
    assert "x_overlay: true" in stored and "x_active: false" in stored
    assert "{{path}}" not in stored  # the text stays the shipped one
    assert injections.render("plan_stale", {"path": "p", "steps": 1}) is None
    assert not injections.active("plan_stale")
    assert injections.param("plan_stale", "steps", 25, int) == 40
    injections.update("plan_stale", active=True)
    assert injections.render("plan_stale", {"path": "p", "steps": 1})


def _edit(source: str, body: str) -> None:
    """The person's text edit of ``source`` (the Prompts page), read at once."""
    shipped = PromptRegistry().resolve(f"{injections.PREFIX}{source}")
    assert shipped is not None
    PromptRegistry().save(shipped.id, text=body, metadata=dict(shipped.metadata))
    path = _override(source)
    stamp = path.stat().st_mtime_ns + 1_000_000
    os.utime(path, ns=(stamp, stamp))


def test_an_edit_on_disk_is_seen_at_once() -> None:
    """The resolution cache is keyed by the override files' modification times."""
    _edit("plan_stale", "Plan {{path}} is quiet.")
    assert injections.render("plan_stale", {"path": "x", "steps": 1}) == "Plan x is quiet."


def test_an_override_with_an_undeclared_placeholder_falls_back_to_the_builtin() -> None:
    """A typo cannot reach the agent: the invalid copy is reported, the built-in is used."""
    _edit("plan_stale", "Plan {{nope}}.")
    text = injections.render("plan_stale", {"path": "x", "steps": 3})
    assert text is not None and text.startswith("The plan (x)")


def test_a_required_reminder_cannot_be_switched_off() -> None:
    """A source that carries data the agent needs can be reworded, never dropped."""
    shipped = next(row for row in _shipped() if row.metadata.get("required"))
    source = shipped.id.removeprefix(injections.PREFIX)
    injections.update(source, active=False)
    assert injections.active(source)
    assert injections.injection(source).required  # type: ignore[union-attr]


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
    injections.update("plan_stale", active=False)
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
    source = shipped.id.removeprefix(injections.PREFIX)
    injections.update(source, active=False)
    path = _override(source)
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^x_active:.*$", 'x_active: false\nx_required: ""', text)
    path.write_text(text, encoding="utf-8")
    stamp = path.stat().st_mtime_ns + 1_000_000
    os.utime(path, ns=(stamp, stamp))
    assert 'x_required: ""' in path.read_text(encoding="utf-8")
    assert injections.active(source)
