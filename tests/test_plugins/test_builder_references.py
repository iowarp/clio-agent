"""The builder's references: every worked example passes ``clio-agent plugin check`` (B8).

Each ``skills/capture/references/<part>.md`` ends with a ``## Worked example`` whose
fenced blocks are tagged ``<language> <path>``: together they are one plugin, written to
a folder and checked as an install would check it. The builder writes plugins from
these examples, so an example that fails the check teaches the builder to fail.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from clio_agent.plugins.check import check
from clio_agent.plugins.service import PluginService
from clio_agent.plugins.store import PluginStore, builtin_records
from tests.test_plugins.helpers import NATIVE

BUILDER = Path(__file__).parents[2] / "src/clio_agent/plugins/builtin/builder"
REFERENCES = BUILDER / "skills/capture/references"
PART_TYPES = {
    "skill",
    "fragment",
    "hook",
    "tool",
    "flow",
    "subagent",
    "command",
    "connection",
    "agent",
}
_BLOCK = re.compile(r"^```(\w+) (\S+)\n(.*?)^```$", re.MULTILINE | re.DOTALL)


def _example(reference: Path) -> dict[str, str]:
    text = reference.read_text(encoding="utf-8")
    _, _, example = text.partition("## Worked example")
    return {path: body for _lang, path, body in _BLOCK.findall(example)}


def test_there_is_one_reference_per_part_type() -> None:
    assert {p.stem for p in REFERENCES.glob("*.md")} == PART_TYPES


@pytest.mark.parametrize("part", sorted(PART_TYPES))
def test_the_worked_example_passes_plugin_check(part: str, tmp_path: Path) -> None:
    files = _example(REFERENCES / f"{part}.md")
    assert "plugin.json" in files, f"{part}.md: the worked example needs a plugin.json block"
    folder = tmp_path / "example"
    for path, body in files.items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    native = NATIVE | {t.name for r in builtin_records() for t in r.tools if t.kind == "native"}
    service = PluginService(PluginStore(tmp_path / "store"), native_tools=native)
    report = check(folder, service=service, trusted=True)
    errors = [d for d in report.diagnostics if d.severity == "error"]
    assert report.ok and not errors, [str(d) for d in errors]
    warnings = [d.code for d in report.diagnostics if d.severity == "warning"]
    assert "fragment_budget" not in warnings and "skill_invalid" not in warnings, warnings


@pytest.mark.parametrize("plugin", ["guide", "builder"])
def test_the_built_in_plugin_passes_plugin_check(plugin: str, tmp_path: Path) -> None:
    native = NATIVE | {t.name for r in builtin_records() for t in r.tools if t.kind == "native"}
    service = PluginService(PluginStore(tmp_path / "store"), native_tools=native)
    report = check(BUILDER.parent / plugin, service=service, trusted=True)
    assert report.ok, [str(d) for d in report.diagnostics]
