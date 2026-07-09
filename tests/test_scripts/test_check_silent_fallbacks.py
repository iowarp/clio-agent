"""Tests for the silent-fallback ratchet (iowarp/clio-agent#772).

Proves the ratchet fails when the violation count EXCEEDS the recorded
baseline and passes when it is at or below it, using a small fixture tree
instead of the real source tree.
"""

from __future__ import annotations

from pathlib import Path

from scripts.check_silent_fallbacks import count_violations, main

_SILENT_FALLBACK = """
def load(path):
    try:
        return open(path).read()
    except Exception:
        pass
"""

_BARE_EXCEPT = """
def probe():
    try:
        return 1 / 0
    except:
        return None
"""

_CLEAN = '''
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b
'''


def _write_fixture_tree(root: Path, *, offenders: int) -> Path:
    """Write ``offenders`` silent-fallback modules plus one clean module."""
    tree = root / "src"
    tree.mkdir(parents=True, exist_ok=True)
    for index in range(offenders):
        (tree / f"offender_{index}.py").write_text(_SILENT_FALLBACK, encoding="utf-8")
    (tree / "clean.py").write_text(_CLEAN, encoding="utf-8")
    return tree


def test_count_violations_per_rule(tmp_path: Path) -> None:
    """Each except-pass offender counts once for BLE001 and once for S110."""
    tree = _write_fixture_tree(tmp_path, offenders=2)
    counts = count_violations(tree)
    assert counts == {"BLE001": 2, "S110": 2, "E722": 0}


def test_count_violations_bare_except(tmp_path: Path) -> None:
    """A bare ``except:`` registers under E722."""
    tree = tmp_path / "src"
    tree.mkdir()
    (tree / "bare.py").write_text(_BARE_EXCEPT, encoding="utf-8")
    counts = count_violations(tree)
    assert counts["E722"] == 1


def test_fails_when_count_exceeds_baseline(tmp_path: Path, capsys) -> None:
    """An increase past the baseline fails the check."""
    tree = _write_fixture_tree(tmp_path, offenders=3)  # 6 violations total
    assert main(["--path", str(tree), "--baseline", "5"]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "total: 6 (baseline: 5)" in out


def test_passes_at_baseline(tmp_path: Path, capsys) -> None:
    """A count exactly at the baseline passes."""
    tree = _write_fixture_tree(tmp_path, offenders=3)
    assert main(["--path", str(tree), "--baseline", "6"]) == 0
    assert "OK" in capsys.readouterr().out


def test_passes_on_decrease_and_prompts_ratchet(tmp_path: Path, capsys) -> None:
    """A count below the baseline passes and asks for the baseline to drop."""
    tree = _write_fixture_tree(tmp_path, offenders=2)  # 4 violations total
    assert main(["--path", str(tree), "--baseline", "6"]) == 0
    out = capsys.readouterr().out
    assert "ratchet it down" in out
    assert "BASELINE_TOTAL = 4" in out


def test_noqa_suppression_is_honored(tmp_path: Path) -> None:
    """A justified, logged site with an explicit noqa leaves the count."""
    tree = tmp_path / "src"
    tree.mkdir()
    (tree / "justified.py").write_text(
        "import logging\n"
        "log = logging.getLogger(__name__)\n"
        "def probe():\n"
        "    try:\n"
        "        return 1\n"
        "    except Exception as exc:  # noqa: BLE001 - probe is best-effort\n"
        '        log.warning("probe failed reason=probe_error error=%s", exc)\n'
        "        return None\n",
        encoding="utf-8",
    )
    counts = count_violations(tree)
    assert counts == {"BLE001": 0, "S110": 0, "E722": 0}
