"""Resolve a discovery checkout to the same immutable revision used by installation."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from clio_agent.gact.blueprint_git import run_git


def inspect_revision(root: Path, *, pin: str, fetched: bool) -> str:
    """Honor a pin in disposable clones and never modify an author's working tree."""
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).strip()
    except subprocess.CalledProcessError as exc:
        if pin:
            raise ValueError("Cannot verify the pinned commit in this source folder") from exc
        return ""
    if pin and commit != pin:
        if not fetched:
            raise ValueError(
                "Source folder HEAD differs from the pin; choose that revision explicitly"
            )
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        run_git(["git", "-C", str(root), "fetch", "--depth", "1", "origin", pin], env=env)
        run_git(["git", "-C", str(root), "checkout", "--detach", pin], env=env)
        commit = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            timeout=10,
        ).strip()
        if commit != pin:
            raise ValueError("Fetched source does not match the pinned commit")
    if (
        pin
        and not fetched
        and subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain"],
            text=True,
            timeout=10,
        ).strip()
    ):
        raise ValueError("Pinned source has uncommitted changes")
    return commit
