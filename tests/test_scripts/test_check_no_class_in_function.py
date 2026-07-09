"""Tests for the class-in-function ratchet (iowarp/clio-agent#774, #714).

Proves the guard fails when a non-baselined file gains a hidden class or a
baselined file exceeds its recorded count, passes at baseline, and reports a
ratchet-down when a baselined file loses violations -- using a small fixture
tree. Also pins the real repository at its recorded baseline.
"""

from __future__ import annotations

from pathlib import Path

from scripts.check_no_class_in_function import (
    RATCHET_BASELINE,
    check_no_class_in_function,
    main,
)

_HIDDEN_CLASS = """
def make():
    class Inner:
        pass
    return Inner
"""

_TWO_HIDDEN = """
def make_a():
    class A:
        pass
    return A


def make_b():
    class B:
        pass
    return B
"""

_MODULE_LEVEL_CLASS = """
class Top:
    def method(self):
        return 1
"""


def _write(tree: Path, rel: str, source: str) -> None:
    """Write a ``rel`` module with ``source``."""
    path = tree / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_module_level_class_is_clean(tmp_path: Path) -> None:
    """A normal module-scope class is not a violation."""
    _write(tmp_path, "ok.py", _MODULE_LEVEL_CLASS)
    result = check_no_class_in_function(tmp_path, baseline={})
    assert not result.failures
    assert not result.ratchet_downs


def test_new_hidden_class_fails(tmp_path: Path) -> None:
    """A hidden class in a non-baselined file is a build-failing offender."""
    _write(tmp_path, "sneaky.py", _HIDDEN_CLASS)
    result = check_no_class_in_function(tmp_path, baseline={})
    assert [f.rel for f in result.failures] == ["sneaky.py"]
    assert result.failures[0].kind == "new"
    assert result.failures[0].count == 1


def test_baselined_at_count_passes(tmp_path: Path) -> None:
    """A baselined file exactly at its recorded count passes."""
    _write(tmp_path, "known.py", _TWO_HIDDEN)
    result = check_no_class_in_function(tmp_path, baseline={"known.py": 2})
    assert not result.failures
    assert not result.ratchet_downs


def test_baselined_above_count_fails(tmp_path: Path) -> None:
    """A baselined file that gained a hidden class regresses and fails."""
    _write(tmp_path, "known.py", _TWO_HIDDEN)
    result = check_no_class_in_function(tmp_path, baseline={"known.py": 1})
    assert [f.rel for f in result.failures] == ["known.py"]
    assert result.failures[0].kind == "regressed"
    assert result.failures[0].baseline == 1
    assert result.failures[0].count == 2


def test_baselined_shrunk_reports_ratchet(tmp_path: Path) -> None:
    """A baselined file with fewer violations: lower the recorded number."""
    _write(tmp_path, "known.py", _HIDDEN_CLASS)  # 1 violation
    result = check_no_class_in_function(tmp_path, baseline={"known.py": 2})
    assert not result.failures
    assert len(result.ratchet_downs) == 1
    entry = result.ratchet_downs[0]
    assert entry.count == 1
    assert entry.cleared is False


def test_baselined_cleared_reports_removal(tmp_path: Path) -> None:
    """A baselined file with zero violations should be dropped from baseline."""
    _write(tmp_path, "known.py", _MODULE_LEVEL_CLASS)  # 0 violations
    result = check_no_class_in_function(tmp_path, baseline={"known.py": 1})
    assert not result.failures
    assert len(result.ratchet_downs) == 1
    assert result.ratchet_downs[0].cleared is True


def test_stale_baseline_entry_reports_ratchet(tmp_path: Path) -> None:
    """A baselined path with no file on disk is a removable stale entry."""
    _write(tmp_path, "present.py", _MODULE_LEVEL_CLASS)
    result = check_no_class_in_function(tmp_path, baseline={"gone.py": 1})
    assert not result.failures
    assert [r.rel for r in result.ratchet_downs] == ["gone.py"]
    assert result.ratchet_downs[0].cleared is True


def test_main_fails_on_new_hidden_class(tmp_path: Path, capsys, monkeypatch) -> None:
    """``main`` exits 1 and names the offending file and class."""
    _write(tmp_path, "sneaky.py", _HIDDEN_CLASS)
    monkeypatch.setattr(
        "scripts.check_no_class_in_function._repo_root", lambda: tmp_path
    )
    monkeypatch.setattr("scripts.check_no_class_in_function.SRC_ROOT", ".")
    monkeypatch.setattr("scripts.check_no_class_in_function.RATCHET_BASELINE", {})
    assert main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "sneaky.py" in out
    assert "Inner" in out


def test_real_tree_holds_at_recorded_baseline() -> None:
    """The live source tree passes at the checked-in baseline (regression pin)."""
    repo_root = Path(__file__).resolve().parents[2]
    result = check_no_class_in_function(repo_root / "src/clio_agent", rel_to=repo_root)
    assert not result.failures, [f._asdict() for f in result.failures]


def test_baseline_entries_all_exist() -> None:
    """Every baselined path must point at a real file (no stale entries)."""
    repo_root = Path(__file__).resolve().parents[2]
    missing = [rel for rel in RATCHET_BASELINE if not (repo_root / rel).is_file()]
    assert not missing, missing
