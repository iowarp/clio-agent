"""Declared workflows: ``ai.iowarp.clio/workflows/*.json`` read, checked and bound."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent.plugins.records import PluginRecord, WorkflowRecord
from clio_agent.plugins.workflows import bind_inputs, read_workflows

DRAFT_REVIEW = {
    "description": "Draft a section, then review it.",
    "inputs": [{"name": "section", "description": "The section to write"}],
    "steps": [
        {"id": "draft", "target": "writer", "input": "Draft the {section} section."},
        {
            "id": "review",
            "target": "reviewer",
            "after": ["draft"],
            "input": {"text": "Review the {section} draft: $draft", "strict": True},
        },
    ],
}


def _write(folder: Path, name: str, data: object) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    (folder / f"{name}.json").write_text(text, encoding="utf-8")


def test_a_declared_workflow_is_read_with_its_inputs_and_steps(tmp_path: Path) -> None:
    _write(tmp_path, "draft-review", DRAFT_REVIEW)
    (record,), problems = read_workflows(tmp_path, "demo")
    assert problems == []
    assert record.id == "draft-review"
    assert record.inputs == (
        {"name": "section", "description": "The section to write", "required": True},
    )
    assert [step["id"] for step in record.steps] == ["draft", "review"]
    # It survives the store's round trip (state.json holds records as dicts).
    stored = PluginRecord.from_dict(
        {"id": "demo", "workflows": [json.loads(json.dumps(record.__dict__))]}
    )
    assert stored.workflows == (record,)


def test_each_bad_workflow_file_is_a_typed_diagnostic(tmp_path: Path) -> None:
    _write(tmp_path, "broken", "{not json")
    _write(tmp_path, "cycle", {"steps": [{"id": "a", "target": "x", "after": ["a"]}]})
    _write(
        tmp_path,
        "nameless",
        {"inputs": [{"description": "?"}], "steps": [{"id": "a", "target": "x"}]},
    )
    _write(tmp_path, "undeclared", {"steps": [{"id": "a", "target": "x", "input": "Do {thing}."}]})
    records, problems = read_workflows(tmp_path, "demo")
    assert records == []
    assert sorted((p.code, p.message.split(":")[0]) for p in problems) == [
        ("workflow_input_undeclared", "undeclared.json"),
        ("workflow_invalid", "cycle.json"),
        ("workflow_invalid", "nameless.json"),
        ("workflow_unreadable", "broken.json"),
    ]
    assert all(p.severity == "error" for p in problems)


def test_binding_fills_every_placeholder_and_names_what_is_missing() -> None:
    record = WorkflowRecord(
        id="draft-review",
        description="",
        path="",
        inputs=(
            {"name": "section", "description": "", "required": True},
            {"name": "tone", "description": "", "required": False},
        ),
        steps=tuple(DRAFT_REVIEW["steps"]),
    )
    steps = bind_inputs(record, {"section": "background"})
    assert steps[0]["input"] == "Draft the background section."
    assert steps[1]["input"] == {"text": "Review the background draft: $draft", "strict": True}
    with pytest.raises(ValueError, match="needs input 'section'"):
        bind_inputs(record, {"tone": "dry"})
