"""Regression tests for iowarp/clio-agent#773: keep pytest ``addopts`` lean.

``addopts`` previously forced ``--verbose`` plus coverage (term-missing +
23MB htmlcov) onto every pytest invocation, including ``--collect-only``.
Coverage belongs to CI, which must pass its flags explicitly.
"""

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_pytest_addopts_is_empty() -> None:
    """pyproject must not force verbose/coverage on every local run (#773)."""
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert pyproject["tool"]["pytest"]["ini_options"]["addopts"] == []


def test_ci_passes_coverage_flags_explicitly() -> None:
    """CI must not rely on addopts for coverage collection (#773)."""
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "--cov=clio_agent" in ci, "CI must enable coverage explicitly"
    floor = re.search(r"--cov-fail-under=(\d+)", ci)
    assert floor is not None, "CI must keep an explicit coverage floor"
    # The floor is a ratchet: it may rise (e.g. 70 -> 78 with --cov-branch,
    # #773 slice 18) but must never drop below the program's original 70.
    assert int(floor.group(1)) >= 70, "CI coverage floor must not ratchet down"
