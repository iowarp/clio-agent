"""Regression tests for iowarp/clio-agent#773: keep pytest ``addopts`` lean.

``addopts`` previously forced ``--verbose`` plus coverage (term-missing +
23MB htmlcov) onto every pytest invocation, including ``--collect-only``.
Coverage belongs to CI, which must pass its flags explicitly.
"""

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
    assert "--cov-fail-under=70" in ci, "CI must keep the coverage floor"
