"""Every test that reads the pinned marketplace goes through ONE guarded path.

A fresh ``git worktree add`` leaves ``external/clio-agent-marketplace`` empty.
Tests that read it used to build the path themselves and crash with an opaque
``FileNotFoundError`` / ``git clone`` error there (or silently skip). These
checks keep the fix structural: the path is built only in ``tests/_marketplace.py``,
every reader is marked ``marketplace`` (or is a live/real-case/integration test
that reports the same message itself), the guard FAILS rather than skips, and
CI both checks the submodule out and asserts it did.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._marketplace import MARKETPLACE_MISSING_MESSAGE, REPO_ROOT, marketplace_checked_out

TESTS = REPO_ROOT / "tests"
_PATH_SEGMENT = re.compile(r'/\s*"clio-agent-marketplace"')
_READER_MARKS = ("mark.marketplace", "mark.real_case", "mark.live", "mark.integration")


def _test_sources() -> list[Path]:
    return [p for p in TESTS.rglob("*.py") if p.name != "_marketplace.py" and p != Path(__file__)]


def test_the_marketplace_path_is_built_only_in_the_helper() -> None:
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for p in _test_sources()
        if _PATH_SEGMENT.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"build the path from tests._marketplace.MARKETPLACE_ROOT: {offenders}"


def test_every_reader_is_marked() -> None:
    unmarked = [
        str(p.relative_to(REPO_ROOT))
        for p in _test_sources()
        if "MARKETPLACE_ROOT" in (text := p.read_text(encoding="utf-8"))
        and not any(mark in text for mark in _READER_MARKS)
    ]
    assert not unmarked, f"mark the tests that read the marketplace: {unmarked}"


class _Item:
    def __init__(self, marked: bool) -> None:
        self._marked = marked

    def get_closest_marker(self, name: str) -> object | None:
        return object() if self._marked and name == "marketplace" else None


def test_a_missing_checkout_fails_with_the_fix_never_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: turn the hook's fail into a skip -> ``Skipped`` is raised, not ``Failed`` -> red."""
    from tests import conftest

    monkeypatch.setattr(conftest, "marketplace_checked_out", lambda: False)
    with pytest.raises(pytest.fail.Exception) as caught:
        conftest.pytest_runtest_setup(_Item(marked=True))  # type: ignore[arg-type]
    assert "git submodule update --init external/clio-agent-marketplace" in str(caught.value)
    conftest.pytest_runtest_setup(_Item(marked=False))  # type: ignore[arg-type]  # unmarked: untouched


def test_a_present_checkout_lets_marked_tests_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests import conftest

    monkeypatch.setattr(conftest, "marketplace_checked_out", lambda: True)
    conftest.pytest_runtest_setup(_Item(marked=True))  # type: ignore[arg-type]


def test_the_real_hook_uses_the_same_check() -> None:
    conftest = (TESTS / "conftest.py").read_text(encoding="utf-8")
    assert 'get_closest_marker("marketplace")' in conftest
    assert "marketplace_checked_out()" in conftest
    assert "pytest.fail(MARKETPLACE_MISSING_MESSAGE" in conftest
    assert (
        "git submodule update --init external/clio-agent-marketplace" in MARKETPLACE_MISSING_MESSAGE
    )


def test_ci_checks_the_submodule_out_and_asserts_it() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "submodules: recursive" in ci
    assert "external/clio-agent-marketplace" in ci  # the explicit assertion step


def test_this_checkout_state_is_reported() -> None:
    """Informational in a fresh worktree; in CI the checkout must be present."""
    import os

    if os.environ.get("CI"):
        assert marketplace_checked_out(), MARKETPLACE_MISSING_MESSAGE
