"""Run a nested pytest session (a probe file) the way CI runs the suite, then clean up.

The hang-guard tests prove their invariant on the real object: a separate pytest
process with the suite conftest (private clio-core daemon, hang guard, bounded store
calls), in which a probe test hangs. That process is ended by the guard, so it never
runs its own teardown; :func:`run_inner_pytest` reaps whatever it left (its private
daemons and run roots) so the outer suite's process-hygiene audit stays clean.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from tests._cte_isolation import reap_stale_suite_runtimes

REPO_ROOT = Path(__file__).resolve().parents[1]


def _inner_env(runtime_parent: Path, extra: Mapping[str, str]) -> dict[str, str]:
    # Drop the outer run's pytest/xdist/coverage wiring: the inner run is its own
    # session, and a coverage hook in a process the guard hard-exits only costs time.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PYTEST_", "COV_CORE_")) and k != "COVERAGE_PROCESS_START"
    }
    env["CLIO_TEST_RUNTIME_ROOT"] = str(runtime_parent)
    env.update(extra)
    return env


@contextmanager
def inner_runtime_parent() -> Iterator[Path]:
    """A short-path parent for nested run roots, reaped and removed on exit.

    Short on purpose: the nested suite builds its own runtime tree below it, which
    overflows Windows' 260-character path limit when nested under ``tmp_path``.
    """
    parent = Path(tempfile.mkdtemp(prefix="in", dir=os.environ["CLIO_TEST_RUNTIME_DIR"]))
    try:
        yield parent
    finally:
        reap_stale_suite_runtimes(parent)
        # A daemon-side path cut at a fixed length can leave a stray empty file here.
        if parent.exists():
            shutil.rmtree(parent)


def run_inner_pytest(
    runtime_parent: Path,
    args: list[str],
    *,
    env: Mapping[str, str] | None = None,
    backstop_s: float,
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m pytest <args>`` from the repo root with the suite conftest.

    Args:
        runtime_parent: From :func:`inner_runtime_parent`.
        args: Pytest arguments (the probe path first).
        env: Extra environment for the inner run.
        backstop_s: Kills the inner run if the guard under test were broken.

    Returns:
        The completed process (text stdout/stderr).
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *args,
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
    ]
    return subprocess.run(  # noqa: S603 - fixed interpreter + in-repo probe
        cmd,
        cwd=REPO_ROOT,
        env=_inner_env(runtime_parent, env or {}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=backstop_s,
    )
