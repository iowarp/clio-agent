"""The ONE path to the pinned marketplace checkout the suite reads, and its guard.

Tests marked ``@pytest.mark.marketplace`` read shipped packs from the
``external/clio-agent-marketplace`` git submodule at the commit this repo
pins: they check what CLIO actually ships (pack prompts, catalogs, workflow
schemas), so a committed copy would test a frozen duplicate instead.

The submodule is empty in a fresh ``git worktree add`` and in a plain
``git clone`` (neither initializes submodules). CI guarantees the checkout
(``actions/checkout`` with ``submodules: recursive`` in ci.yml, plus an explicit
assertion step). Where it is missing, a marked test FAILS with the fix
(``tests/conftest.py`` ``pytest_runtest_setup``) -- never a skip, which would
let a CI checkout regression pass silently.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE_ROOT = REPO_ROOT / "external" / "clio-agent-marketplace"

MARKETPLACE_MISSING_MESSAGE = (
    "the marketplace submodule is not checked out at external/clio-agent-marketplace "
    "(a fresh `git worktree add` or a plain `git clone` leaves submodules empty). Run:\n"
    "    git submodule update --init external/clio-agent-marketplace\n"
    "CI checks it out with actions/checkout `submodules: recursive`."
)


def marketplace_checked_out() -> bool:
    """Whether the submodule is populated (its packs are present on disk)."""
    return MARKETPLACE_ROOT.is_dir() and any(MARKETPLACE_ROOT.glob("*/AGENT.md"))


__all__ = [
    "MARKETPLACE_MISSING_MESSAGE",
    "MARKETPLACE_ROOT",
    "REPO_ROOT",
    "marketplace_checked_out",
]
