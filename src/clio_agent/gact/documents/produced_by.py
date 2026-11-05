"""The "Produced by" line: which tool call and script made an artifact version.

A comment on a figure or page a script made is about the script (comments plan, rule 3), so
the entry names the call and the script from the version's recorded transforms. A
``run_code`` producer is named by its cell. When the agent registered the file itself with
``create_artifact``, the script comes from that call's own record (ledger FB30). When no
transform is recorded, the line says the producer is unknown rather than leaving it out.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.artifacts.registry import get_registry

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.records import ArtifactVersion

__all__ = ["ProducedBy", "produced_by"]

_SCRIPT_SUFFIXES = {".py", ".sh", ".r", ".jl", ".js", ".ts", ".m"}
_CELL_LINES = 12


@dataclass(frozen=True)
class ProducedBy:
    """The header lines naming a version's producer, and the script when one is known."""

    lines: tuple[str, ...]
    script: str = ""

    @property
    def known(self) -> bool:
        """Whether a recorded tool call made the version."""
        return not self.lines[0].startswith("Producer unknown")


def _display(path: str, workspace_root: Path | None) -> str:
    if not path:
        return ""
    candidate = Path(path)
    if workspace_root is not None and candidate.is_absolute():
        try:
            return candidate.resolve(strict=False).relative_to(workspace_root).as_posix()
        except ValueError:
            return candidate.as_posix()
    return candidate.as_posix()


def _workspace_root(app: "FastAPI", workspace_id: str) -> Path | None:
    workspace = app.state.workspaces.get(workspace_id)
    root = str(getattr(workspace, "root_path", "") or "") if workspace else ""
    return Path(root).resolve(strict=False) if root else None


def _script(record: Any, registry: Any, root: Path | None) -> str:
    instrument = record.instrument
    if instrument.script_artifact_id:
        found = registry.get_by_artifact_id(instrument.script_artifact_id)
        if found is not None:
            _logical, version = found
            return _display(version.path or _logical.name, root)
    for edge in record.used:
        name = edge.path or edge.name
        if Path(name).suffix.lower() in _SCRIPT_SUFFIXES:
            return _display(name, root)
    return ""


def _reads(record: Any, script: str, root: Path | None) -> list[str]:
    names: list[str] = []
    for edge in record.used:
        name = _display(edge.path or edge.name or edge.external_ref, root)
        if name and name != script and name not in names:
            names.append(name)
    return names[:3]


def produced_by(
    app: "FastAPI", version: "ArtifactVersion", *, workspace_id: str, noun: str = "image"
) -> ProducedBy:
    """The "Produced by" lines for ``version``; ``noun`` names what not to edit by hand."""

    registry = get_registry(app)
    records = list(registry.transforms_generating(version.artifact_id))
    if not records:
        return ProducedBy(("Producer unknown: no recorded tool call made this version.",))
    record = records[-1]
    root = _workspace_root(app, workspace_id)
    tool = record.instrument.tool or "a tool"
    call = record.call_id
    if tool.endswith("run_code"):
        code = str(record.instrument.args.get("code") or "").strip("\n")
        cell = code.splitlines()
        shown = "\n".join(f"    {line}" for line in cell[:_CELL_LINES])
        more = (
            f"\n    ... ({len(cell) - _CELL_LINES} more lines)" if len(cell) > _CELL_LINES else ""
        )
        return ProducedBy(
            (
                f"Produced by: run_code call {call} running this cell:",
                shown + more,
                f"Change the cell and run it again, not the {noun}.",
            ),
            script="the run_code cell",
        )
    script = _script(record, registry, root)
    reads = _reads(record, script, root)
    read_text = f" (reads {', '.join(reads)})" if reads else ""
    if script:
        return ProducedBy(
            (
                f"Produced by: {tool} call {call} running {script}{read_text}.",
                f"Change the script, not the {noun}.",
            ),
            script=script,
        )
    if tool.endswith("create_artifact"):
        return ProducedBy(
            (
                f"Produced by: the agent registered this file with create_artifact call {call}; "
                "no script is recorded for it.",
            )
        )
    return ProducedBy((f"Produced by: {tool} call {call}{read_text}.",))
