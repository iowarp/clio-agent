""" "Produced by" for a file the agent wrote in one call and registered with another (K4)."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from clio_agent.gact.documents.produced_by import produced_by


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _record(call_id: str, tool: str, start: datetime, end: datetime, **args: Any) -> Any:
    return SimpleNamespace(
        call_id=call_id,
        session_id="sess_1",
        instrument=SimpleNamespace(tool=tool, args=args, script_artifact_id=""),
        used=[],
        started_at=_iso(start),
        ended_at=_iso(end),
    )


def _app(tmp_path: Path, session: list[Any], generating: list[Any]) -> Any:
    registry = SimpleNamespace(
        capture_released=None,
        transforms_generating=lambda _artifact_id: generating,
        transforms_for_session=lambda _session_id: session,
        get_by_artifact_id=lambda _artifact_id: None,
    )
    workspaces = {"ws": SimpleNamespace(root_path=str(tmp_path))}
    return SimpleNamespace(state=SimpleNamespace(artifact_registry=registry, workspaces=workspaces))


def _figure(tmp_path: Path, written: datetime) -> Any:
    path = tmp_path / "ratio.png"
    path.write_bytes(b"png bytes")
    os.utime(path, (written.timestamp(), written.timestamp()))
    return SimpleNamespace(
        artifact_id="artifact_ratio",
        path=str(path),
        sha256=hashlib.sha256(b"png bytes").hexdigest(),
    )


def test_the_run_code_cell_that_wrote_a_registered_file_is_named(tmp_path: Path) -> None:
    t0 = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    cell = _record(
        "call_cell", "run_code", t0, t0 + timedelta(seconds=5), code="plt.savefig('ratio.png')"
    )
    register = _record(
        "call_reg", "create_artifact", t0 + timedelta(seconds=9), t0 + timedelta(seconds=10)
    )
    version = _figure(tmp_path, t0 + timedelta(seconds=3))
    made = produced_by(_app(tmp_path, [cell, register], [register]), version, workspace_id="ws")
    assert made.lines[0] == (
        "Produced by: run_code call call_cell running this cell (the file was then registered "
        "with create_artifact call call_reg):"
    )
    assert "plt.savefig('ratio.png')" in made.lines[1]
    assert made.known and made.script == "the run_code cell"


def test_no_writer_is_guessed_when_two_calls_ran_or_the_file_changed(tmp_path: Path) -> None:
    t0 = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    one = _record("call_a", "run_code", t0, t0 + timedelta(seconds=5), code="a")
    two = _record("call_b", "shell_bash", t0, t0 + timedelta(seconds=5), command="b")
    register = _record(
        "call_reg", "create_artifact", t0 + timedelta(seconds=9), t0 + timedelta(seconds=10)
    )
    version = _figure(tmp_path, t0 + timedelta(seconds=3))
    both = produced_by(_app(tmp_path, [one, two, register], [register]), version, workspace_id="ws")
    assert "no script is recorded for it" in both.lines[0]
    Path(version.path).write_bytes(b"changed since")  # the write time no longer says who wrote it
    changed = produced_by(_app(tmp_path, [one, register], [register]), version, workspace_id="ws")
    assert "no script is recorded for it" in changed.lines[0]


def test_the_shell_command_and_the_registered_inputs_name_the_script(tmp_path: Path) -> None:
    # Scenario L20: the figure was drawn by a shell call and registered with
    # used=[the script, the CSV]. Neither was a registered file, so no used edge exists;
    # the entry still repeats the command and what the agent said the figure was made from.
    t0 = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    shell = _record(
        "call_sh",
        "shell_bash",
        t0,
        t0 + timedelta(seconds=5),
        command="cd paper && python3 scripts/plot_fig.py\n  && ls figs",
    )
    register = _record(
        "call_reg",
        "create_artifact",
        t0 + timedelta(seconds=9),
        t0 + timedelta(seconds=10),
        used=[str(tmp_path / "scripts" / "plot_fig.py"), "throughput_model.csv"],
    )
    version = _figure(tmp_path, t0 + timedelta(seconds=3))
    made = produced_by(_app(tmp_path, [shell, register], [register]), version, workspace_id="ws")
    assert made.lines == (
        "Produced by: shell_bash call call_sh (the file was then registered with "
        "create_artifact call call_reg).",
        "It ran: cd paper && python3 scripts/plot_fig.py && ls figs",
        "Registered as made from scripts/plot_fig.py, throughput_model.csv.",
        "Change the script, not the image.",
    )
    assert made.script == "scripts/plot_fig.py"
    # With no single writer, the registered inputs still name the script.
    other = _record("call_x", "shell_bash", t0, t0 + timedelta(seconds=5), command="x")
    both = produced_by(
        _app(tmp_path, [shell, other, register], [register]), version, workspace_id="ws"
    )
    assert both.lines[0] == (
        "Produced by: the agent registered this file with create_artifact call call_reg, as "
        "made from scripts/plot_fig.py, throughput_model.csv."
    )
    assert both.script == "scripts/plot_fig.py"


def test_a_rerun_version_says_how_the_files_earlier_version_was_made(tmp_path: Path) -> None:
    # Scenario L20: the agent reran its script four times; the paper's builds recorded the
    # new figure versions with no producing call, so a comment on version 5 said only
    # "Producer unknown". It now says how version 1 was made, as version 1's.
    t0 = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    register = _record(
        "call_reg",
        "create_artifact",
        t0,
        t0 + timedelta(seconds=1),
        used=["scripts/plot_fig.py", "throughput_model.csv"],
    )
    first = SimpleNamespace(artifact_id="artifact_v1", version=1, path="", sha256="")
    fifth = SimpleNamespace(artifact_id="artifact_v5", version=5, path="", sha256="")
    logical = SimpleNamespace(versions=[first, fifth])
    registry = SimpleNamespace(
        capture_released=None,
        transforms_generating=lambda artifact_id: [register]
        if artifact_id == "artifact_v1"
        else [],
        transforms_for_session=lambda _session_id: [register],
        get_by_artifact_id=lambda artifact_id: (logical, fifth),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            artifact_registry=registry,
            workspaces={"ws": SimpleNamespace(root_path=str(tmp_path))},
        )
    )
    made = produced_by(app, fifth, workspace_id="ws")
    assert made.lines[0] == (
        "Producer unknown for this version (5); version 1 of this file was made this way:"
    )
    assert "as made from scripts/plot_fig.py, throughput_model.csv" in made.lines[1]
    assert made.script == "scripts/plot_fig.py" and not made.known
