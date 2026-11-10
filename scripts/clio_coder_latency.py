"""Measure real delegated runs without changing the product's result or event contract."""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import psutil

from clio_agent.runtime.clio_coder import delegate as delegation
from clio_agent.runtime.clio_coder import drivers
from clio_agent.runtime.clio_coder.launch import ClioCoderInstall, InstanceHome
from clio_agent.runtime.clio_coder.workspace import Workspace, remove_worktree

TASK = "calc.py's add() returns the wrong answer. Please fix it so add(2, 3) == 5."
FOLLOW_UP = "Also add sub(a, b), returning a minus b, without changing add."


def git(repo: Path, *args: str, input_text: str | None = None) -> str:
    """Run Git only in the demo's disposable repository."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def fixture_repo(root: Path) -> Path:
    """Make a new, small repository; never overwrite an existing demo."""
    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=False)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "calc.py")
    git(
        repo,
        "-c",
        "user.name=CLIO demo",
        "-c",
        "user.email=demo@invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "test: seed addition bug",
    )
    return repo


class PhaseProbe:
    """Scoped measurement wrappers used only by the sequential demo process.

    Full headless JSON is requested to observe token deltas. Production delegation
    uses terminal JSON, which does not expose a first-token timestamp.
    """

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.seconds: dict[str, float] = {}
        self.marks: dict[str, float] = {}
        self.stack = contextlib.ExitStack()

    def mark(self, name: str) -> None:
        self.marks.setdefault(name, time.monotonic() - self.started)

    def sync(self, owner: Any, name: str, phase: str) -> None:
        original = getattr(owner, name)

        def measured(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            try:
                return original(*args, **kwargs)
            finally:
                self.seconds[phase] = self.seconds.get(phase, 0) + time.monotonic() - start

        self.stack.enter_context(patch.object(owner, name, measured))

    def asynchronous(self, owner: Any, name: str, phase: str) -> None:
        original = getattr(owner, name)

        async def measured(*args: Any, **kwargs: Any) -> Any:
            if phase == "spawn":
                self.mark("spawn_start")
            start = time.monotonic()
            try:
                return await original(*args, **kwargs)
            finally:
                self.seconds[phase] = self.seconds.get(phase, 0) + time.monotonic() - start

        self.stack.enter_context(patch.object(owner, name, measured))

    def event(self, event: dict[str, Any]) -> None:
        if event.get("kind") == "session":
            self.mark("session")
        if event.get("kind") == "text" and event.get("text"):
            self.mark("first_token")

    def __enter__(self) -> PhaseProbe:
        self.sync(ClioCoderInstall, "resolve", "binary_resolution")
        self.sync(ClioCoderInstall, "help_flags", "capability_probe")
        self.sync(InstanceHome, "prepare", "scoped_home")
        self.sync(delegation, "create_worktree", "worktree")
        self.sync(delegation, "collect_changes", "handback")
        self.sync(delegation, "commit_changes", "handback")
        self.asynchronous(drivers, "spawn_instance", "spawn")
        self.asynchronous(delegation, "run_verification", "check")
        original_args = drivers.headless_args
        original_fold = drivers._fold_headless_frame

        def full_args(*args: Any, **kwargs: Any) -> list[str]:
            argv = original_args(*args, **kwargs)
            argv[argv.index("--json-events") + 1] = "full"
            return argv

        def fold(result: Any, frame: dict[str, Any], sink: Any) -> None:
            if frame.get("type") in ("text_delta", "thinking_delta") and frame.get("delta"):
                self.mark("first_token")
            if frame.get("type") == "turn_end" and "message" in frame:
                result.text = "".join(
                    block.get("text", "")
                    for block in frame["message"].get("content", [])
                    if block.get("type") == "text"
                )
            original_fold(result, frame, sink)

        self.stack.enter_context(patch.object(drivers, "headless_args", full_args))
        self.stack.enter_context(patch.object(drivers, "_fold_headless_frame", fold))
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stack.__exit__(*exc)

    def report(self) -> dict[str, Any]:
        spawn = self.marks.get("spawn_start")
        session = self.marks.get("session")
        token = self.marks.get("first_token")
        return {
            **self.seconds,
            "boot_to_session": None if spawn is None or session is None else session - spawn,
            "session_to_first_token": (
                None if session is None or token is None else token - session
            ),
            "clio_decides": None,
            "total": time.monotonic() - self.started,
            "marks": self.marks,
        }


async def measured_run(root: Path, template: Path, target: str, model: str, surface: str) -> dict:
    """Run, independently check, hand back, and apply one fresh delegated change."""
    repo = fixture_repo(root)
    check = "import calc; assert calc.add(2, 3) == 5"
    if surface == "acp":
        check += "; assert calc.sub(5, 3) == 2"
    request = delegation.DelegationRequest(
        task=TASK,
        repo=repo,
        state_root=root / "delegation",
        surface=surface,
        follow_ups=[FOLLOW_UP] if surface == "acp" else [],
        home_template=template,
        options=drivers.RunOptions(target=target, model=model, timeout=180),
        verify_command=[sys.executable, "-B", "-c", check],
    )
    result = None
    with PhaseProbe() as probe:
        try:
            result = await delegation.delegate_coding_task(request, sink=probe.event)
            accepted = result.outcome == "succeeded" and result.verified and bool(result.commit)
            applied = False
            if accepted:
                start = time.monotonic()
                git(repo, "apply", "--check", input_text=result.diff)
                git(repo, "apply", input_text=result.diff)
                subprocess.run([sys.executable, "-B", "-c", check], cwd=repo, check=True)
                probe.seconds["apply"] = time.monotonic() - start
                applied = True
            return {
                "surface": surface,
                "model": model,
                "target": target,
                "passed": accepted and applied,
                "phases_s": probe.report(),
                "result": result.to_dict(),
                "measurement": "direct runtime; no parent routing; headless uses full JSON",
            }
        finally:
            if result is not None and result.branch:
                remove_worktree(
                    Workspace(Path(result.workspace), repo, result.branch, result.base_commit)
                )


async def cancel_run(root: Path, template: Path, target: str, model: str) -> dict[str, Any]:
    """Cancel a real spawned headless session and confirm worktree cleanup."""
    repo = fixture_repo(root)
    ready = asyncio.Event()
    instances = []
    original_spawn = drivers.spawn_instance

    async def tracked_spawn(*args: Any, **kwargs: Any) -> Any:
        instance = await original_spawn(*args, **kwargs)
        instances.append(instance)
        return instance

    request = delegation.DelegationRequest(
        task=TASK,
        repo=repo,
        state_root=root / "delegation",
        home_template=template,
        options=drivers.RunOptions(target=target, model=model, timeout=180),
    )
    with patch.object(drivers, "spawn_instance", tracked_spawn):
        work = asyncio.create_task(
            delegation.delegate_coding_task(
                request,
                sink=lambda event: ready.set() if event.get("kind") == "session" else None,
            )
        )
        try:
            await asyncio.wait_for(ready.wait(), timeout=60)
            if work.done():
                return {"passed": False, "reason": "run finished before cancellation"}
            leader = psutil.Process(instances[0].process.pid)
            owned = [leader, *leader.children(recursive=True)]
            identities = [(proc, proc.create_time()) for proc in owned]
            work.cancel()
            try:
                await work
            except asyncio.CancelledError:
                clean = len(git(repo, "worktree", "list", "--porcelain").split("worktree ")) == 2
                survivors = []
                for proc, born in identities:
                    try:
                        if (
                            proc.create_time() == born
                            and proc.is_running()
                            and proc.status() != psutil.STATUS_ZOMBIE
                        ):
                            survivors.append(proc.pid)
                    except psutil.NoSuchProcess:
                        continue
                if survivors:
                    leaked = [proc for proc, _born in identities if proc.pid in survivors]
                    for proc in leaked:
                        with contextlib.suppress(psutil.NoSuchProcess):
                            proc.terminate()
                    _gone, alive = await asyncio.to_thread(psutil.wait_procs, leaked, timeout=5)
                    for proc in alive:
                        with contextlib.suppress(psutil.NoSuchProcess):
                            proc.kill()
                    await asyncio.to_thread(psutil.wait_procs, alive, timeout=5)
                return {
                    "passed": clean
                    and not survivors
                    and instances[0].process.returncode is not None,
                    "reason": "canceled after session creation",
                    "observed_processes": [proc.pid for proc, _born in identities],
                    "survivors": survivors,
                }
            return {"passed": False, "reason": "cancellation was not propagated"}
        finally:
            if not work.done():
                work.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                result = await work
                if result.branch:
                    remove_worktree(
                        Workspace(Path(result.workspace), repo, result.branch, result.base_commit)
                    )


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
