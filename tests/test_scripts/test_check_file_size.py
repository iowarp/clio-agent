"""Tests for the per-file line-count ratchet (iowarp/clio-agent#774, #714).

Proves the guard fails when a non-baselined file exceeds the cap or a baselined
file grows past its recorded count, passes when every file is at or below its
limit, and reports a ratchet-down when a baselined file shrinks -- using a small
fixture tree instead of the real source tree. Also pins the real repository at
its recorded baseline so a drift fails CI.
"""

from __future__ import annotations

from pathlib import Path

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
    result = check_file_size(tmp_path, max_lines=800, baseline={})
    assert [f.rel for f in result.failures] == ["big.py"]
    assert result.failures[0].kind == "new"
    assert not result.ratchet_downs


def test_new_file_under_cap_is_clean(tmp_path: Path) -> None:
    """A non-baselined file under the cap does not offend."""
    _write(tmp_path, "small.py", 400)
    result = check_file_size(tmp_path, max_lines=800, baseline={})
    assert not result.failures
    assert not result.ratchet_downs


def test_baselined_file_at_baseline_passes(tmp_path: Path) -> None:
    """A baselined file exactly at its recorded count passes."""
    _write(tmp_path, "known.py", 1200)
    result = check_file_size(tmp_path, max_lines=800, baseline={"known.py": 1200})
    assert not result.failures
    assert not result.ratchet_downs


def test_baselined_file_above_baseline_fails(tmp_path: Path) -> None:
    """A baselined file that grew past its recorded count regresses and fails."""
    _write(tmp_path, "known.py", 1300)
    result = check_file_size(tmp_path, max_lines=800, baseline={"known.py": 1200})
    assert [f.rel for f in result.failures] == ["known.py"]
    assert result.failures[0].kind == "regressed"
    assert result.failures[0].limit == 1200


def test_baselined_file_shrunk_but_over_cap_reports_ratchet(tmp_path: Path) -> None:
    """A baselined file that shrank but is still over the cap: lower the number."""
    _write(tmp_path, "known.py", 1000)
    result = check_file_size(tmp_path, max_lines=800, baseline={"known.py": 1200})
    assert not result.failures
    assert len(result.ratchet_downs) == 1
    entry = result.ratchet_downs[0]
    assert entry.rel == "known.py"
    assert entry.count == 1000
    assert entry.under_cap is False


def test_baselined_file_under_cap_reports_removal(tmp_path: Path) -> None:
    """A baselined file that fell under the cap should be dropped from baseline."""
    _write(tmp_path, "known.py", 500)
    result = check_file_size(tmp_path, max_lines=800, baseline={"known.py": 1200})
    assert not result.failures
    assert len(result.ratchet_downs) == 1
    assert result.ratchet_downs[0].under_cap is True


def test_main_reports_ratchet_down_message(tmp_path: Path, capsys, monkeypatch) -> None:
    """``main`` prints an actionable ratchet-down line and still exits 0."""
    _write(tmp_path, "known.py", 500)
    monkeypatch.setattr("scripts.check_file_size._repo_root", lambda: tmp_path)
    monkeypatch.setattr("scripts.check_file_size.SRC_ROOT", ".")
    monkeypatch.setattr(
        "scripts.check_file_size.RATCHET_BASELINE", {"known.py": 1200}
    )
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "ratchet down" in out
    assert "remove it from RATCHET_BASELINE" in out


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
    result = check_file_size(repo_root / "src/clio_agent", rel_to=repo_root)
    assert not result.failures, [f._asdict() for f in result.failures]


def test_baseline_entries_all_exist() -> None:
    """Every baselined path must point at a real file (no stale entries)."""
    repo_root = Path(__file__).resolve().parents[2]
    missing = [rel for rel in RATCHET_BASELINE if not (repo_root / rel).is_file()]
    assert not missing, missing
