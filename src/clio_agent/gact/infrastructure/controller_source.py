"""Which code this CLIO runs, so a remote CLIO it deploys runs the same (owner module).

A published release is named by its version: a remote installs that release from
PyPI. A CLIO built from any other commit (a source install, or a desktop built from a
branch) shares its version number with a release but not its code, so a remote must
install that commit from source instead.

The desktop's bundled runtime records its commit in ``runtime.json`` at build time
(``install/build-gact-runtime.sh``): ``{"source": {"commit": ..., "release": ...}}``.
A source install answers from its own git checkout.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

#: The checkout root when clio-agent runs from source (``src/clio_agent/gact/infrastructure``).
_CHECKOUT = Path(__file__).resolve().parents[4]


def controller_commit() -> str | None:
    """The commit this CLIO was built from, or None when it is a published release."""

    bundled = os.environ.get("GACT_BUNDLED_RUNTIME_DIR", "").strip()
    if bundled:
        return _bundled_commit(Path(bundled) / "runtime.json")
    if (_CHECKOUT / ".git").exists() and (_CHECKOUT / "pyproject.toml").is_file():
        return _checkout_commit(_CHECKOUT)
    return None


def _bundled_commit(manifest: Path) -> str | None:
    try:
        source = json.loads(manifest.read_text(encoding="utf-8")).get("source") or {}
    except (OSError, ValueError):
        return None  # a runtime built before it recorded its source: treat as a release
    commit = str(source.get("commit") or "")
    return None if source.get("release") or not commit else commit


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _checkout_commit(checkout: Path) -> str | None:
    commit = _git(checkout, "rev-parse", "HEAD")
    if not commit:
        return None
    # A checkout exactly at a release tag runs that release.
    tag = _git(checkout, "describe", "--exact-match", "--tags", "HEAD")
    return None if tag.startswith("v") else commit
