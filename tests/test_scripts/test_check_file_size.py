"""Tests for the per-file line-count ratchet (iowarp/clio-agent#774, #714).

Proves the guard fails when a non-baselined file exceeds the cap, when a
baselined file's count differs from its entry in either direction, and when an
entry names a file that no longer exists (#1203) -- using a small fixture tree
instead of the real source tree. Also pins the real repository at its recorded
baseline so a drift fails CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_file_size import (
    RATCHET_BASELINE,
    check_file_size,
    main,
)


def _write(tree: Path, rel: str, lines: int) -> None:
    """Write a ``rel`` module with ``lines`` newline-terminated lines."""
    path = tree / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n" * lines, encoding="utf-8")


def test_new_file_over_cap_fails(tmp_path: Path) -> None:
    """A non-baselined file over the cap is a build-failing offender."""
    _write(tmp_path, "big.py", 900)
    failures = check_file_size(tmp_path, max_lines=800, baseline={})
    assert [(f.rel, f.kind) for f in failures] == [("big.py", "new")]


def test_new_file_under_cap_is_clean(tmp_path: Path) -> None:
    """A non-baselined file under the cap does not offend."""
    _write(tmp_path, "small.py", 400)
    assert check_file_size(tmp_path, max_lines=800, baseline={}) == []


def test_baselined_file_at_baseline_passes(tmp_path: Path) -> None:
    """A baselined file exactly at its recorded count passes."""
    _write(tmp_path, "known.py", 1200)
    assert check_file_size(tmp_path, max_lines=800, baseline={"known.py": 1200}) == []


def test_baselined_file_above_baseline_fails(tmp_path: Path) -> None:
    """A baselined file that grew past its recorded count regresses and fails."""
    _write(tmp_path, "known.py", 1300)
    failures = check_file_size(tmp_path, max_lines=800, baseline={"known.py": 1200})
    assert [(f.rel, f.kind, f.limit) for f in failures] == [("known.py", "regressed", 1200)]


def test_shrunk_file_over_cap_fails_until_its_entry_is_lowered(tmp_path: Path) -> None:
    """A shrink is not banked as headroom: the entry must drop to the new count."""
    _write(tmp_path, "known.py", 1000)
    failures = check_file_size(tmp_path, max_lines=800, baseline={"known.py": 1200})
    assert [(f.rel, f.count, f.kind, f.limit) for f in failures] == [
        ("known.py", 1000, "padded", 1200)
    ]


def test_shrunk_file_under_cap_fails_until_its_entry_is_removed(tmp_path: Path) -> None:
    _write(tmp_path, "known.py", 500)
    failures = check_file_size(tmp_path, max_lines=800, baseline={"known.py": 1200})
    assert [(f.kind, f.count) for f in failures] == [("padded", 500)]


def test_growth_and_baseline_bump_in_one_commit_fails(tmp_path: Path) -> None:
    """#1203 sabotage: grow a file AND raise its entry past the growth. Fails."""
    _write(tmp_path, "known.py", 1210)  # grew by 10
    failures = check_file_size(tmp_path, max_lines=800, baseline={"known.py": 1250})
    assert [(f.kind, f.count, f.limit) for f in failures] == [("padded", 1210, 1250)]


def test_entry_for_a_missing_file_is_stale(tmp_path: Path) -> None:
    """A deleted/renamed file's allowance must go, or a new file could inherit it."""
    _write(tmp_path, "kept.py", 10)
    failures = check_file_size(tmp_path, max_lines=800, baseline={"gone.py": 1200})
    assert [(f.rel, f.count, f.kind, f.limit) for f in failures] == [
        ("gone.py", None, "stale", 1200)
    ]


@pytest.mark.parametrize(
    ("lines", "baseline", "expected"),
    [
        (
            500,
            {"known.py": 1200},
            "known.py:500 (baseline claims 1200 but the file has 500 lines "
            "-- remove its RATCHET_BASELINE entry)",
        ),
        (1000, {"known.py": 1200}, "lower its RATCHET_BASELINE entry to 1000"),
        (10, {"gone.py": 1200}, "gone.py (baselined at 1200 lines but the file no longer"),
    ],
)
def test_main_fails_and_names_the_fix(
    tmp_path: Path, capsys, monkeypatch, lines: int, baseline: dict[str, int], expected: str
) -> None:
    _write(tmp_path, "known.py", lines)
    monkeypatch.setattr("scripts.check_file_size._repo_root", lambda: tmp_path)
    monkeypatch.setattr("scripts.check_file_size.SRC_ROOT", ".")
    monkeypatch.setattr("scripts.check_file_size.RATCHET_BASELINE", baseline)
    assert main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert expected in out


def test_main_fails_on_new_offender(tmp_path: Path, capsys, monkeypatch) -> None:
    """``main`` exits 1 and names a fresh over-cap file."""
    _write(tmp_path, "big.py", 900)
    monkeypatch.setattr("scripts.check_file_size._repo_root", lambda: tmp_path)
    monkeypatch.setattr("scripts.check_file_size.SRC_ROOT", ".")
    monkeypatch.setattr("scripts.check_file_size.RATCHET_BASELINE", {})
    assert main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "big.py:900" in out


def test_real_tree_holds_at_recorded_baseline() -> None:
    """The live source tree passes at the checked-in baseline (regression pin)."""
    repo_root = Path(__file__).resolve().parents[2]
    failures = check_file_size(repo_root / "src/clio_agent", rel_to=repo_root)
    assert not failures, [f._asdict() for f in failures]


def test_baseline_entries_all_exist() -> None:
    """Every baselined path must point at a real file (no stale entries)."""
    repo_root = Path(__file__).resolve().parents[2]
    missing = [rel for rel in RATCHET_BASELINE if not (repo_root / rel).is_file()]
    assert not missing, missing
