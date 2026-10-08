"""Fingerprint the source actually available before a live service starts."""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any


def source_identity(root: Path) -> dict[str, Any]:
    """Hash tracked and new runtime source, including the separate UI checkout."""

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=root)

    names = (
        git(
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "src",
            "scripts/live_verification",
        )
        .decode()
        .splitlines()
    )
    files = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in sorted(set(names))
        if (root / name).is_file()
    }
    ui = root / "external/gact-tui"
    ui_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ui).decode().strip()
    ui_diff = subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=ui)
    return {
        "head": git("rev-parse", "HEAD").decode().strip(),
        "branch": git("branch", "--show-current").decode().strip(),
        "diff_sha256": hashlib.sha256(git("diff", "--binary", "HEAD")).hexdigest(),
        "runtime_files": files,
        "ui_head": ui_head,
        "ui_diff_sha256": hashlib.sha256(ui_diff).hexdigest(),
        "python": sys.executable,
        "python_version": platform.python_version(),
        "libraries": {
            name: importlib.metadata.version(name) for name in ("fastmcp", "dspy", "mcp")
        },
    }
