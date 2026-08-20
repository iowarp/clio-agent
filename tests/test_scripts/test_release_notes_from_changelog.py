"""The release-notes backstop extracts exactly the tag's CHANGELOG section (#release-page heal)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "release_notes_from_changelog.py"


def _run(tag: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), tag], capture_output=True, text=True, check=False
    )


def test_extracts_current_release_section() -> None:
    """The released version's section is found (leading v optional) and links the CHANGELOG."""
    import tomllib

    version = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    out = _run(f"v{version}")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip()
    assert "CHANGELOG" in out.stdout
    # The section body must not bleed into the NEXT release's heading.
    assert "\n## [" not in out.stdout


def test_unknown_tag_exits_nonzero_and_prints_nothing() -> None:
    """A tag without a CHANGELOG section fails loudly (the workflow leaves the body alone)."""
    out = _run("v9.9.9")
    assert out.returncode == 1
    assert out.stdout == ""
