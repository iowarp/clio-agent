"""The "Produced by" line: which tool call and script made an artifact version.

A comment on a figure or page a script made is about the script (comments plan, rule 3), so
the entry names the call and the script from the version's recorded transforms. A
``run_code`` producer is named by its cell. When the agent registered the file itself with
``create_artifact``, the script comes from that call's own record (ledger FB30). When no
transform is recorded, the line says the producer is unknown rather than leaving it out.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.artifacts.registry import get_registry

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.records import ArtifactVersion

__all__ = ["ProducedBy", "produced_by"]

_SCRIPT_SUFFIXES = {".py", ".sh", ".r", ".jl", ".js", ".ts", ".m"}
_CELL_LINES = 12
#: How much of a shell command the entry repeats.
_COMMAND_CHARS = 200
#: A file's write time may land just after its call's recorded end.
_WRITE_SLACK = timedelta(seconds=2)


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


def _declared(record: Any, root: Path | None) -> list[str]:
    """The inputs a ``create_artifact`` call said the file was made from, as it named them.

    A ``used`` ref that names no registered file records no edge (declared_used_edges), but
    it is what the agent said; the entry repeats it as that, with the call that wrote the file.
    """
    from clio_agent.gact.artifacts.declared_used_edges import (  # noqa: PLC0415
        create_artifact_used_refs,
    )

    names: list[str] = []
    for ref in create_artifact_used_refs(record.instrument.args):
        name = _display(ref, root)
        if name and name not in names:
            names.append(name)
    return names[:4]


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _writer(app: "FastAPI", version: "ArtifactVersion", registered_by: Any) -> Any:
    """The call that wrote a file the agent then registered, read from the disk.

    ``create_artifact`` records no producing call, but the file says when it was written:
    while its bytes are still the registered version, the one earlier call of the session
    that was running at that moment wrote it. ``None`` when that is not exactly one call.
    """

    path = Path(version.path or "")
    try:
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != version.sha256:
            return None
        written = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
    running = []
    for record in get_registry(app).transforms_for_session(registered_by.session_id):
        if record.call_id == registered_by.call_id or record.instrument.tool.endswith(
            "create_artifact"
        ):
            continue
        started, ended = _parse_time(record.started_at), _parse_time(record.ended_at)
        if started and ended and started <= written <= ended + _WRITE_SLACK:
            running.append(record)
    return running[0] if len(running) == 1 else None


def _cell(record: Any, noun: str, registered: str = "") -> ProducedBy:
    code = str(record.instrument.args.get("code") or "").strip("\n")
    cell = code.splitlines()
    shown = "\n".join(f"    {line}" for line in cell[:_CELL_LINES])
    more = f"\n    ... ({len(cell) - _CELL_LINES} more lines)" if len(cell) > _CELL_LINES else ""
    return ProducedBy(
        (
            f"Produced by: run_code call {record.call_id} running this cell{registered}:",
            shown + more,
            f"Change the cell and run it again, not the {noun}.",
        ),
        script="the run_code cell",
    )


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
        return _cell(record, noun)
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
        writer = _writer(app, version, record)
        registered = f" (the file was then registered with create_artifact call {call})"
        if writer is not None and writer.instrument.tool.endswith("run_code"):
            return _cell(writer, noun, registered)
        declared = _declared(record, root)
        named = next((n for n in declared if Path(n).suffix.lower() in _SCRIPT_SUFFIXES), "")
        made_from = [f"Registered as made from {', '.join(declared)}."] if declared else []
        if writer is not None:
            command = " ".join(str(writer.instrument.args.get("command") or "").split())
            ran = [f"It ran: {command[:_COMMAND_CHARS]}"] if command else []
            return ProducedBy(
                (
                    f"Produced by: {writer.instrument.tool} call {writer.call_id}{registered}.",
                    *ran,
                    *made_from,
                    f"Change {'the script' if named else 'what that call ran'}, not the {noun}.",
                ),
                script=named,
            )
        if named:
            return ProducedBy(
                (
                    f"Produced by: the agent registered this file with create_artifact call "
                    f"{call}, as made from {', '.join(declared)}.",
                    f"Change the script, not the {noun}.",
                ),
                script=named,
            )
        return ProducedBy(
            (
                f"Produced by: the agent registered this file with create_artifact call {call}; "
                "no script is recorded for it.",
            )
        )
    return ProducedBy((f"Produced by: {tool} call {call}{read_text}.",))
