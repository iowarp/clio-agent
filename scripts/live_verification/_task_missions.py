"""Owned inputs and explicit missions for model-driven task qualification."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any

import _common as common


def shell_input(workspace: Path) -> str:
    """Create an actual finite process with observable output and filesystem completion."""
    (workspace / "owned-shell.py").write_text(
        "import time,pathlib\n"
        "print('owned shell started',flush=True)\n"
        "for i in range(45):\n print(f'progress {i}',flush=True);time.sleep(1)\n"
        "pathlib.Path('shell-done.txt').write_text('owned shell completed')\n"
        "print('owned shell completed',flush=True)\n",
        encoding="utf-8",
    )
    command = common.quoted_command(sys.executable, str(workspace / "owned-shell.py"))
    return "& " + command if os.name == "nt" else command


def folder_input(proof: Path, kind: str) -> dict[str, Any]:
    """Create real owned files, below the production enumeration limit, without a fake adapter."""
    root = proof / "upstream"
    root.mkdir()
    payload = b"owned source payload\n" * 100_003
    selected = root / "payload.bin"
    selected.write_bytes(payload)
    seed = root / "seed.txt"
    seed.write_bytes(b"owned indexing entry\n" * 32)
    count = 48_000 if kind == "Indexing" else 24_000
    for index in range(count):
        directory = root / f"group-{index // 500:03}"
        directory.mkdir(exist_ok=True)
        destination = directory / f"entry-{index:06}.txt"
        if index % 500 == 0:
            destination.write_bytes(seed.read_bytes())
        else:
            # NTFS caps hard links per file. Each real seed owns at most 500.
            os.link(directory / f"entry-{index // 500 * 500:06}.txt", destination)
    return {
        "root": str(root),
        "entries": count + (count + 499) // 500 + 2,
        "selected_path": "payload.bin",
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def mission(kind: str, workspace: Path, *, source: dict[str, Any] | None = None) -> str:
    """Name actual tools and require independent work before any explicit wait."""
    read = f"fs_read_file(filepath={str(workspace / 'sentinel.txt')!r})"
    if kind in {"Shell", "Subagent"}:
        command = shell_input(workspace)
        execution = (
            f"shell_bash(command={command!r}, background={kind == 'Shell'}, timeout_s=0, "
            f"cwd={str(workspace)!r})"
        )
        submit = (
            f"Call {execution}."
            if kind == "Shell"
            else "Call spawn_agent_task(agent='worker', task="
            + repr(f"Call {execution}. Report actual stdout and the final exit code.")
            + ")."
        )
    elif kind == "Indexing" and source is not None:
        submit = (
            f"Call connected_data_connect(provider='local', root={source['root']!r}, "
            "label='Owned live indexing', mode='read_only')."
        )
    elif kind == "Download" and source is not None:
        submit = (
            f"Call connected_data_download(source_id={source['source_id']!r}, "
            "selected_paths=['payload.bin'], description='Download owned live payload')."
        )
    else:
        raise ValueError(f"No mission for {kind}")
    return (
        submit + f" Immediately call query_tasks(kind={kind!r}) and {read}. "
        "Call wait_tasks on the accepted handle with timeout_s=0.1. If it expires, do not "
        "cancel or relaunch. Call observe_tasks on that handle, then wait_tasks without a "
        "timeout. Finally call get_task_result on that handle. Report actual returned "
        "observations, including errors, independently read text and the final result. "
        "Never invent a handle or infer that a task is running without a snapshot."
    )
