"""Node folders of an isolated workflow run.

A workflow started with ``isolate: true`` gets ``<workspace>/runs/<run-id>/``, and
every node session gets its own folder, recorded in its metadata (``node_folder``):

* a step: ``<run-id>/<step-id>/``;
* a fork: beside its forker, ``<step-id>~1/`` and so on, starting as a copy of the
  forker's folder at the moment of the fork (cloned where the filesystem allows,
  :mod:`clio_agent.runtime.file_clone`; the method is recorded as ``folder_copy``);
* a child a node delegates to: nested under the node's folder.

While a node's tool call runs, its folder narrows the session's write territory for
both the file policy and the shell's fence (:mod:`clio_agent.tools.write_scope`); its
shell starts there and its prompt names it. A run is refused at start with a typed
reason when the OS fence cannot narrow, rather than running unconfined in silence.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: Session metadata naming the node's folder.
NODE_FOLDER_KEY = "node_folder"
#: Session metadata recording how a fork's folder was copied.
FOLDER_COPY_KEY = "folder_copy"
#: The folder under the workspace root that holds isolated runs.
RUNS_DIRNAME = "runs"
#: Typed reason: isolation was asked for and the OS fence cannot narrow writes.
ISOLATION_UNCONFINED = "isolation_unconfined"

_UNSAFE = re.compile(r"[^A-Za-z0-9_.~-]+")


def _segment(text: str, fallback: str = "node") -> str:
    """One safe path segment from a label (``..`` and separators never survive)."""

    cleaned = _UNSAFE.sub("-", str(text or "")).strip(".-")[:64]
    return cleaned or fallback


def session_node_folder(app: Any, session_id: str) -> str:
    """The node folder of ``session_id``, created if missing, or ``""``."""

    sessions = getattr(getattr(app, "state", None), "sessions", None)
    session = sessions.get(session_id) if sessions is not None and session_id else None
    folder = str((getattr(session, "metadata", None) or {}).get(NODE_FOLDER_KEY) or "")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
    return folder


def node_confinement(app: Any, session_id: str) -> dict[str, Any]:
    """A native submission's confinement: the session's node folder, when it has one."""

    folder = session_node_folder(app, session_id)
    return {"write_root": folder} if folder else {}


def isolation_refusal() -> dict[str, Any] | None:
    """A typed refusal when the OS fence cannot narrow a node's writes, else ``None``."""

    from clio_agent.runtime import sandbox  # noqa: PLC0415

    state = sandbox.current_state()
    if state is not None and state.active and state.mechanism != sandbox.MECHANISM_NONE:
        return None
    return {
        "accepted": False,
        "reason": ISOLATION_UNCONFINED,
        "error": (
            "isolated runs need the OS write fence, which is not active here ("
            + (state.reason if state is not None else "not installed")
            + "); run without isolate, or set up the fence (clio sandbox setup)"
        ),
    }


def run_folder(workspace_root: str | Path, run_id: str) -> Path:
    """``<workspace>/runs/<run-id>/``, created."""

    folder = Path(workspace_root).expanduser().resolve() / RUNS_DIRNAME / _segment(run_id, "run")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def step_folder(run: str | Path, step_id: str) -> str:
    """The folder of step ``step_id`` in the run folder ``run``."""

    return str(Path(run) / _segment(step_id, "step"))


def _claim(parent: Path, name: str) -> Path:
    """Create and return ``parent/name``, or ``name-2``, ``name-3``... when it is taken."""

    parent.mkdir(parents=True, exist_ok=True)
    candidate, number = parent / name, 1
    while True:
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            number += 1
            candidate = parent / f"{name}-{number}"


def _fork_name(source: Path, label: str) -> str:
    """``<step>~<n>`` for a workflow step's fork (its label), else ``<name>~<n>``."""

    if label and "~" in label:
        return _segment(label)
    number = 1
    while (source.parent / f"{source.name}~{number}").exists():
        number += 1
    return f"{source.name}~{number}"


def child_node_metadata(
    parent_metadata: dict[str, Any],
    child_metadata: dict[str, Any],
    source_metadata: dict[str, Any] | None,
    title: str,
) -> dict[str, Any]:
    """The node folder (and, for a fork, the copy record) a new child session gets.

    ``child_metadata`` is what the child already carries (a workflow step names its
    folder); ``source_metadata`` is the forked session's, for a fork.
    """

    from clio_agent.runtime.file_clone import clone_or_copy_tree  # noqa: PLC0415

    source_folder = str((source_metadata or {}).get(NODE_FOLDER_KEY) or "")
    if source_folder:
        source = Path(source_folder)
        label = str(child_metadata.get("subagent_name") or "")
        folder = _claim(source.parent, _fork_name(source, label))
        copied = (
            clone_or_copy_tree(source, folder)
            if source.is_dir()
            else {"method": "empty", "cloned": 0, "copied": 0}
        )
        return {
            NODE_FOLDER_KEY: str(folder),
            FOLDER_COPY_KEY: {**copied, "from": str(source)},
        }
    named = str(child_metadata.get(NODE_FOLDER_KEY) or "")
    if named:
        Path(named).mkdir(parents=True, exist_ok=True)
        return {NODE_FOLDER_KEY: named}
    parent_folder = str(parent_metadata.get(NODE_FOLDER_KEY) or "")
    if parent_folder:
        label = str(child_metadata.get("subagent_name") or title)
        return {NODE_FOLDER_KEY: str(_claim(Path(parent_folder), _segment(label)))}
    return {}


def prompt_lines(folder: str) -> list[str]:
    """What a node's prompt says about its folder."""

    return [
        f"Your node folder: {folder}",
        (
            "This session is one node of an isolated workflow run. Your shell starts in "
            "your node folder, and relative output paths land there. You may write only "
            "inside it; you may read the whole workspace."
        ),
    ]


__all__ = [
    "FOLDER_COPY_KEY",
    "ISOLATION_UNCONFINED",
    "NODE_FOLDER_KEY",
    "child_node_metadata",
    "isolation_refusal",
    "node_confinement",
    "prompt_lines",
    "run_folder",
    "session_node_folder",
    "step_folder",
]
