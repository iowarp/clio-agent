"""Build the H1 and H2 repositories and check that their seeded behaviour holds."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from scripts.eval.tool_calling.fixtures import repo_h1, repo_h2
from scripts.eval.tool_calling.fixtures.common import build_history, write_state


def _state_at(commits: list[tuple[str, Mapping[str, str | None]]], index: int) -> dict[str, str]:
    state: dict[str, str] = {}
    for _, files in commits[: index + 1]:
        for rel, text in files.items():
            if text is None:
                state.pop(rel, None)
            else:
                state[rel] = text
    return state


def tests_pass(state: Mapping[str, str], scratch: Path) -> bool:
    """Run the state's unittest suite in a scratch folder."""
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    try:
        write_state(scratch, state)
        result = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
            cwd=scratch,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return result.returncode == 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _build(module: ModuleType, dest: Path, scratch: Path, *, head_passes: bool) -> dict[str, Any]:
    commits = module.COMMITS
    shas = build_history(dest, commits)
    before = tests_pass(_state_at(commits, module.REGRESSION_INDEX - 1), scratch)
    head = tests_pass(_state_at(commits, len(commits) - 1), scratch)
    if not before or head != head_passes:
        raise RuntimeError(
            f"{dest.name}: seeded behaviour does not hold "
            f"(tests before regression pass={before}, at HEAD pass={head})"
        )
    return {
        **module.ANSWER,
        "commits": [
            {"sha": sha, "message": msg} for sha, (msg, _) in zip(shas, commits, strict=True)
        ],
        "introducing_commit": shas[module.REGRESSION_INDEX],
        "head": shas[-1],
        "verified": {"tests_pass_before_regression": before, "tests_pass_at_head": head},
    }


def build_h1(dest: Path, scratch: Path) -> dict[str, Any]:
    """The ledgerlite repository; its tests fail at HEAD."""
    return _build(repo_h1, dest, scratch, head_passes=False)


def build_h2(dest: Path, scratch: Path) -> dict[str, Any]:
    """The recstream repository; its tests pass at HEAD, but reads are slow."""
    return _build(repo_h2, dest, scratch, head_passes=True)
