"""Default locations. All runtime state stays under the checkout's ``.cloud-run/``."""

from __future__ import annotations

from pathlib import Path

#: The clio-agent checkout this harness lives in.
REPO_ROOT = Path(__file__).resolve().parents[3]
STATE_ROOT = REPO_ROOT / ".cloud-run"
DEFAULT_FIXTURES = STATE_ROOT / "eval-fixtures"
DEFAULT_RESULTS = STATE_ROOT / "eval-results"
DEFAULT_WORK = STATE_ROOT / "eval-work"
MANIFEST_NAME = "manifest.json"
#: The marketplace the fixture generator writes inside the fixtures folder.
MARKETPLACE_DIR = "marketplace"
MARKETPLACE_NAME = "clio-eval"


def manifest_path(fixtures: Path) -> Path:
    """The manifest inside a fixtures folder."""
    return fixtures / MANIFEST_NAME
