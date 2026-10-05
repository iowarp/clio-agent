"""Explicit, path-scoped Git publication of blueprint authoring edits."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def publish_git_changes(
    source: Path, files: list[str], *, message: str, push: bool
) -> dict[str, str | bool]:
    """Commit only the reviewed blueprint paths; push only when explicitly requested.

    Other staged work is preserved. A push uses the checkout's configured
    upstream and never forces, resets, or changes its branch. Git failure leaves
    the saved source files and any local commit intact for inspection/retry.
    """
    if not message.strip():
        raise ValueError("A commit message is required for Git publication")

    def git(*args: str) -> str:
        completed = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(source), *args],
            capture_output=True,
            text=True,
            timeout=120,
            env={
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
            },
        )
        if completed.returncode:
            # Git's remote output can contain embedded credentials. Do not send
            # that output to the transcript or the browser's notification log.
            raise ValueError(
                f"Git {args[0]} failed (exit {completed.returncode}). Source edits are saved; inspect the checkout on this CLIO before retrying."
            )
        return completed.stdout.strip()

    repository = Path(git("rev-parse", "--show-toplevel")).resolve()
    selected = []
    for relative in files:
        target = (source / relative).resolve()
        target.relative_to(source.resolve())
        selected.append(target.relative_to(repository).as_posix())
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD")
    if selected and git("status", "--porcelain", "--", *files):
        # --only keeps unrelated pre-staged changes out of this commit.
        git("add", "--", *files)
        git("commit", "--only", "-m", message, "--", *files)
    commit = git("rev-parse", "HEAD")
    if push:
        git("push")
    return {"commit": commit, "branch": branch, "pushed": push}
