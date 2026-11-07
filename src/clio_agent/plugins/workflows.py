"""The workflows a plugin declares: ``ai.iowarp.clio/workflows/*.json`` (owner module).

One file is one workflow a person can ``@``-summon and an agent can run by id with
``run_workflow(workflow="<id>", inputs={...})``::

    {
      "description": "Draft a section, then review it.",
      "inputs": [{"name": "section", "description": "The section to write", "required": true}],
      "steps": [
        {"id": "draft", "target": "wtfp-section-writer", "input": "Draft the {section} section."},
        {"id": "review", "target": "wtfp-section-reviewer", "after": ["draft"],
         "input": "Review the {section} draft: $draft"}
      ]
    }

The id is the file's stem (or its ``id`` field). Steps are the shapes
:class:`clio_agent.sdk.workflow.Workflow` validates; ``{name}`` in a step's input is
replaced by that input's value when the workflow runs. ``plugin check`` reports a
malformed file, invalid steps, and a placeholder naming no declared input.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clio_agent.plugins.records import Diagnostic, WorkflowRecord

#: ``{name}`` in a step input: a declared input's value at run time.
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_-]*)\}")


def _diag(plugin: str, code: str, message: str, path: Path) -> Diagnostic:
    return Diagnostic("error", code, f"{path.name}: {message}", plugin)


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _texts(item)]
    if isinstance(value, list):
        return [text for item in value for text in _texts(item)]
    return []


def placeholders(steps: Any) -> set[str]:
    """Every ``{name}`` the steps' inputs use."""

    rows = steps if isinstance(steps, list) else []
    return {
        name
        for step in rows
        if isinstance(step, Mapping)
        for text in _texts(step.get("input"))
        for name in _PLACEHOLDER.findall(text)
    }


def read_workflows(folder: Path, plugin: str) -> tuple[list[WorkflowRecord], list[Diagnostic]]:
    """Read ``folder``'s workflow files; each problem is a typed diagnostic."""

    from clio_agent.sdk.workflow import Workflow, WorkflowError  # noqa: PLC0415

    records: list[WorkflowRecord] = []
    diagnostics: list[Diagnostic] = []
    if not folder.is_dir():
        return records, diagnostics
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            diagnostics.append(_diag(plugin, "workflow_unreadable", str(exc), path))
            continue
        if not isinstance(data, dict):
            diagnostics.append(_diag(plugin, "workflow_invalid", "not a JSON object", path))
            continue
        steps = data.get("steps")
        try:
            Workflow.from_dict({"steps": steps})
        except WorkflowError as exc:
            diagnostics.append(_diag(plugin, "workflow_invalid", str(exc), path))
            continue
        inputs = [
            {
                "name": str(row.get("name") or "").strip(),
                "description": str(row.get("description") or ""),
                "required": bool(row.get("required", True)),
            }
            for row in data.get("inputs") or []
            if isinstance(row, Mapping)
        ]
        if any(not row["name"] for row in inputs):
            diagnostics.append(_diag(plugin, "workflow_invalid", "an input has no name", path))
            continue
        undeclared = sorted(placeholders(steps) - {row["name"] for row in inputs})
        if undeclared:
            diagnostics.append(
                _diag(
                    plugin,
                    "workflow_input_undeclared",
                    "a step uses "
                    + ", ".join("{" + n + "}" for n in undeclared)
                    + ", which no input declares",
                    path,
                )
            )
            continue
        records.append(
            WorkflowRecord(
                id=str(data.get("id") or path.stem).strip(),
                description=str(data.get("description") or ""),
                path=str(path),
                inputs=tuple(inputs),
                steps=tuple(steps),
            )
        )
    return records, diagnostics


def bind_inputs(record: WorkflowRecord, values: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The record's steps with each ``{name}`` replaced by its input's value.

    Raises:
        ValueError: a required input has no value (the message names it), or a value
            names no declared input.
    """

    given = {str(k): v for k, v in (values or {}).items()}
    declared = {row["name"] for row in record.inputs}
    unknown = sorted(set(given) - declared)
    if unknown:
        raise ValueError(f"workflow {record.id!r} has no input " + ", ".join(map(repr, unknown)))
    missing = [
        row["name"]
        for row in record.inputs
        if row.get("required") and not str(given.get(row["name"], "")).strip()
    ]
    if missing:
        raise ValueError(f"workflow {record.id!r} needs input " + ", ".join(map(repr, missing)))

    def fill(value: Any) -> Any:
        if isinstance(value, str):
            return _PLACEHOLDER.sub(
                lambda m: str(given.get(m.group(1), "")) if m.group(1) in declared else m.group(0),
                value,
            )
        if isinstance(value, Mapping):
            return {key: fill(item) for key, item in value.items()}
        if isinstance(value, list):
            return [fill(item) for item in value]
        return value

    return [{**step, "input": fill(step.get("input"))} for step in record.steps]


def resolution_workflows(resolution: Any) -> list[tuple[str, WorkflowRecord]]:
    """``(plugin, workflow)`` for every workflow the resolution's plugins declare.

    Read from each resolved plugin's own folder (a pinned snapshot), so a resolution
    digest does not change because workflows exist.
    """

    from clio_agent.plugins.manifest import NAMESPACE  # noqa: PLC0415

    found: list[tuple[str, WorkflowRecord]] = []
    for row in getattr(resolution, "plugins", None) or []:
        root = str(row.get("root") or "")
        if root:
            records, _problems = read_workflows(Path(root) / NAMESPACE / "workflows", row["id"])
            found.extend((str(row["id"]), record) for record in records)
    return found


def find_workflow(resolution: Any, ref: str) -> tuple[str, WorkflowRecord] | None:
    """The workflow ``ref`` names (``<id>`` or ``<plugin>/<id>``), or ``None``."""

    plugin, _, name = ref.strip().rpartition("/")
    for owner, record in resolution_workflows(resolution):
        if record.id == name and plugin in ("", owner):
            return owner, record
    return None


__all__ = [
    "bind_inputs",
    "find_workflow",
    "placeholders",
    "read_workflows",
    "resolution_workflows",
]
