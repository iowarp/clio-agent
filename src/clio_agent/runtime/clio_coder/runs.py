"""Run records: what a resumable Clio Coder delegation needs between CLIO turns.

A run is one Clio Coder ACP session working in one worktree on one branch. Every
``run_clio_coder`` call is a turn of a run; a later call continues it by id, and landing or
discarding it ends it. The record lives next to the worktrees, under the delegation's state
root, and holds only what git and Clio Coder do not: the session id, the acceptance check and
each turn's verdict.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from clio_agent.runtime.clio_coder.launch import REASON_WORKSPACE, ClioCoderError
from clio_agent.runtime.clio_coder.workspace import Workspace

RUN_OPEN = "open"
RUN_LANDED = "landed"
RUN_DISCARDED = "discarded"
REASON_RUN = "clio_coder_run"
_RUN_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,79}")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def valid_run_id(run_id: str) -> bool:
    return bool(_RUN_ID.fullmatch(run_id)) and ".." not in run_id


@dataclass
class RunTurn:
    """One turn's hand-back, as the land step reads it."""

    task: str
    outcome: str
    outcome_detail: str | None
    commit: str | None
    verified: bool | None
    usage: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=_now)


@dataclass
class RunRecord:
    run_id: str
    repo: str
    worktree: str
    branch: str
    base_commit: str
    session_id: str | None = None
    verify_command: list[str] | None = None
    verify_timeout: float = 120.0
    status: str = RUN_OPEN
    turns: list[RunTurn] = field(default_factory=list)
    landed_into: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def workspace(self) -> Workspace:
        return Workspace(
            path=Path(self.worktree),
            repo=Path(self.repo),
            branch=self.branch,
            base_commit=self.base_commit,
        )

    @property
    def tip(self) -> str | None:
        """The newest commit any turn handed back, if one did."""
        return next((turn.commit for turn in reversed(self.turns) if turn.commit), None)

    def acceptance_gap(self) -> str | None:
        """Why the run may not land yet, or ``None`` when its last turn was accepted."""
        if self.status != RUN_OPEN:
            return f"run {self.run_id} is already {self.status}"
        if not self.turns:
            return f"run {self.run_id} has no finished turn"
        last = self.turns[-1]
        if last.outcome != "succeeded":
            return f"its last turn did not succeed ({last.outcome})"
        if not self.verify_command:
            return "it has no acceptance check; continue it with a verify_command first"
        if not last.verified:
            return "the acceptance check did not pass on its last turn"
        if self.tip is None:
            return "it changed nothing"
        return None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
        turns = [RunTurn(**turn) for turn in data.get("turns", [])]
        return cls(**{**data, "turns": turns})


def run_path(state_root: Path, run_id: str) -> Path:
    if not valid_run_id(run_id):
        raise ClioCoderError(REASON_RUN, f"not a Clio Coder run id: {run_id!r}")
    return state_root / "runs" / f"{run_id}.json"


def save_run(state_root: Path, record: RunRecord) -> None:
    """Write atomically, so a crash never leaves a half record."""
    record.updated_at = _now()
    path = run_path(state_root, record.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".json.partial")
    partial.write_text(json.dumps(record.to_dict(), indent=2), "utf-8")
    os.replace(partial, path)


@contextlib.contextmanager
def run_lock(state_root: Path, run_id: str) -> Iterator[None]:
    """Hold the run exclusively: one turn, land or discard at a time, across processes."""
    path = run_path(state_root, run_id).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise ClioCoderError(
            REASON_RUN, f"run {run_id} is busy with another turn", {"run": run_id}
        ) from None
    try:
        yield
    finally:
        lock.release()


def load_run(state_root: Path, run_id: str) -> RunRecord:
    path = run_path(state_root, run_id)
    try:
        return RunRecord.from_dict(json.loads(path.read_text("utf-8")))
    except FileNotFoundError:
        raise ClioCoderError(
            REASON_RUN, f"no Clio Coder run {run_id} in this workspace", {"run": run_id}
        ) from None
    except (ValueError, TypeError) as exc:
        raise ClioCoderError(
            REASON_WORKSPACE, f"the record of run {run_id} is unreadable", {"path": str(path)}
        ) from exc


def new_run(workspace: Workspace, run_id: str, verify: Sequence[str] | None, timeout: float):
    if workspace.repo is None or workspace.branch is None or workspace.base_commit is None:
        raise ClioCoderError(REASON_RUN, "only a worktree delegation can be a resumable run")
    return RunRecord(
        run_id=run_id,
        repo=str(workspace.repo),
        worktree=str(workspace.path),
        branch=workspace.branch,
        base_commit=workspace.base_commit,
        verify_command=list(verify) if verify else None,
        verify_timeout=timeout,
    )
