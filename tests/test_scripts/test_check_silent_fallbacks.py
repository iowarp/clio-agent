"""Tests for the silent-fallback ratchet (iowarp/clio-agent#772).

Proves the ratchet fails when the violation count EXCEEDS the recorded
baseline and passes when it is at or below it, using a small fixture tree
instead of the real source tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import scripts.check_silent_fallbacks as guard
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


def test_count_survives_forced_colour_env(tmp_path: Path, monkeypatch) -> None:
    """Counting is correct even when the env forces coloured ruff output.

    Regression for iowarp/clio-agent#772: with ``FORCE_COLOR`` set, ruff wrote
    ANSI escapes into ``--statistics`` output that the parser could not match,
    so it returned zeros and the ratchet reported a clean tree while real
    violations existed. The subprocess must scrub colour from its own env.
    """
    monkeypatch.setenv("FORCE_COLOR", "3")
    monkeypatch.delenv("NO_COLOR", raising=False)
    tree = _write_fixture_tree(tmp_path, offenders=2)
    counts = count_violations(tree)
    assert counts == {"BLE001": 2, "S110": 2, "E722": 0}


def test_raises_when_violations_but_unparseable(monkeypatch) -> None:
    """A verdict/parse contradiction raises instead of returning zeros.

    If ruff signals violations (exit 1) but no statistics line parses -- e.g.
    a future format or colour regression survives ANSI stripping -- swallowing
    that as ``{...: 0}`` would silently disarm the ratchet. It must be a hard
    RuntimeError carrying the raw stdout (iowarp/clio-agent#772).
    """
    mangled = "\x1b\x1b garbage that is not a statistics line\n"

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=["ruff"], returncode=1, stdout=mangled, stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="exit 1"):
        count_violations(Path("does-not-matter"))


def test_raises_when_mangled_stat_line_under_exit_zero(monkeypatch) -> None:
    """A statistics-SHAPED but unparseable line raises even under exit 0.

    Defense in depth for iowarp/clio-agent#772: a leading-count line whose rule
    column is mangled (survives ANSI stripping but no longer parses into a rule
    code) means the count it reported reads as zero. That is a parse failure, not
    a clean tree, regardless of ruff's exit code -- so it raises.
    """

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["ruff"], returncode=0, stdout="7\t?mangled?\tmystery\n", stderr=""
        )

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="did not parse into a"):
        count_violations(Path("does-not-matter"))


def test_raises_on_heterogeneous_partial_parse(monkeypatch) -> None:
    """One good line + one mangled line (sum>0) still raises (iowarp/clio-agent#772).

    The old guard only raised on a TOTAL wipeout (exit 1 AND every count zero).
    If ruff's format changes so one selected rule's line still parses (sum>0) but
    another's is mangled, the mangled rule is silently undercounted as zero. The
    guard must raise whenever ANY statistics-shaped line fails to parse into a
    known rule, regardless of the total.
    """

    stdout = "2\tBLE001\tblind-except\n3\t?mangled?\tsome-rule\n"

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=["ruff"], returncode=1, stdout=stdout, stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="did not parse into a"):
        count_violations(Path("does-not-matter"))


def test_well_formed_unselected_rule_line_does_not_raise(monkeypatch) -> None:
    """A well-formed line for a rule we did NOT select is ignored, not suspicious.

    Guards against over-triggering (iowarp/clio-agent#772): ``7\\tXYZ999`` parses
    cleanly as a rule code, just not one in the ratchet set, so it is valid ruff
    output -- ignored, never counted, never raised on.
    """

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["ruff"], returncode=0, stdout="7\tXYZ999\tmystery\n", stderr=""
        )

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    assert count_violations(Path("does-not-matter")) == {"BLE001": 0, "S110": 0, "E722": 0}


def test_clean_tree_does_not_raise(tmp_path: Path) -> None:
    """A genuinely clean tree (exit 0, no statistics) returns zeros without raising."""
    tree = tmp_path / "src"
    tree.mkdir()
    (tree / "clean.py").write_text(_CLEAN, encoding="utf-8")
    assert count_violations(tree) == {"BLE001": 0, "S110": 0, "E722": 0}


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
