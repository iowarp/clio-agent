"""Tests for the per-file ratchet on broad excepts hidden behind ``noqa``.

``check_silent_fallbacks.py`` honours ``noqa``, so it read 0 while 583 broad excepts sat
behind ``# noqa: BLE001`` (the clio-core fail-stop audit, 2026-09-30). This guard counts
them with ``noqa`` ignored, per file, against a baseline that may only go down.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.check_noqa_swallows import count_hidden_by_file, main

_HIDDEN = (
    "def probe():\n"
    "    try:\n"
    "        return 1\n"
    "    except Exception:  # noqa: BLE001,S110 - looks justified\n"
    "        pass\n"
)


def _tree(root: Path, files: dict[str, int]) -> Path:
    tree = root / "src"
    tree.mkdir(parents=True, exist_ok=True)
    for name, hidden in files.items():
        (tree / name).write_text(_HIDDEN * hidden if hidden else "x = 1\n", encoding="utf-8")
    return tree


def _baseline(root: Path, counts: dict[str, int]) -> Path:
    path = root / "baseline.json"
    path.write_text(json.dumps(counts), encoding="utf-8")
    return path


def test_hidden_excepts_are_counted_per_file(tmp_path: Path) -> None:
    tree = _tree(tmp_path, {"a.py": 2, "b.py": 0})

    counts = count_hidden_by_file(tree)

    # Each hidden site is a BLE001 and an S110 (try/except/pass).
    assert counts == {"a.py": 4}


def test_a_file_above_its_baseline_fails(tmp_path: Path, capsys) -> None:
    tree = _tree(tmp_path, {"a.py": 2})

    assert main(["--path", str(tree), "--baseline", str(_baseline(tmp_path, {"a.py": 2}))]) == 1
    assert "a.py: 4 > 2" in capsys.readouterr().out


def test_a_new_file_with_a_hidden_except_fails(tmp_path: Path, capsys) -> None:
    tree = _tree(tmp_path, {"new.py": 1})

    assert main(["--path", str(tree), "--baseline", str(_baseline(tmp_path, {}))]) == 1
    assert "new.py: 2 > 0" in capsys.readouterr().out


def test_moving_swallows_between_files_still_fails(tmp_path: Path) -> None:
    """The total may fall while one file grows: per file, not per tree."""
    tree = _tree(tmp_path, {"a.py": 2, "b.py": 0})

    assert (
        main(["--path", str(tree), "--baseline", str(_baseline(tmp_path, {"a.py": 0, "b.py": 9}))])
        == 1
    )


def test_at_or_below_the_baseline_passes_and_prompts_the_ratchet(tmp_path: Path, capsys) -> None:
    tree = _tree(tmp_path, {"a.py": 1})

    assert main(["--path", str(tree), "--baseline", str(_baseline(tmp_path, {"a.py": 6}))]) == 0
    assert "lower-baseline" in capsys.readouterr().out


def test_lower_baseline_only_lowers(tmp_path: Path) -> None:
    tree = _tree(tmp_path, {"a.py": 1, "b.py": 3})
    path = _baseline(tmp_path, {"a.py": 6, "b.py": 2})

    assert main(["--path", str(tree), "--baseline", str(path), "--lower-baseline"]) == 1

    assert json.loads(path.read_text(encoding="utf-8")) == {"a.py": 2, "b.py": 2}
