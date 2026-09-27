"""A stuck clio-core daemon fails the test in seconds; it never hangs the run.

The clio-core binding holds the GIL for the whole of a blocking RPC (PR #1473), so a
daemon that stops answering freezes the test process; before the hang guard that cost
up to GitHub's 6-hour job limit. This runs ``_stuck_daemon_probe.py`` in a fresh
pytest subprocess (own private daemon, own runtime root), where the probe suspends its
daemon and blocks in a real ``ClioCoreStore.get``. The per-call bound
(``tests/_cte_bounded.py`` on ``tests/_hang_guard.py``'s C watchdog) must end that
process within seconds, with every thread's stack naming the stuck store op.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import psutil
import pytest

from tests._cte_isolation import cte_isolation_available
from tests._inner_suite import REPO_ROOT, inner_runtime_parent, run_inner_pytest

PROBE = REPO_ROOT / "tests" / "test_arc" / "_stuck_daemon_probe.py"
CALL_BOUND_S = 3.0
# Headroom between the bound firing and the outer test observing the exit: the dump,
# interpreter exit, and a loaded runner. Far below any hang this test exists to catch.
EXIT_SLACK_S = 12.0
# Backstop only, if the hang guard were broken: the inner run is killed here and the
# test fails. Covers interpreter start, suite conftest import and a cold daemon spawn.
INNER_RUN_BACKSTOP_S = 150.0

pytestmark = pytest.mark.skipif(
    not cte_isolation_available(),
    reason="needs the clio-core binding and launcher to run a private daemon",
)


def _resume_suspended_daemon(probe_out: Path) -> None:
    """Resume the probe's daemon so the reap's polite stop can act on it at once."""
    try:
        pid = int(json.loads(probe_out.read_text(encoding="utf-8"))["daemon_pid"])
    except (OSError, KeyError, ValueError):
        return  # the probe never suspended it
    try:
        psutil.Process(pid).resume()
    except psutil.NoSuchProcess:
        pass


@pytest.mark.timeout(INNER_RUN_BACKSTOP_S + 60)  # spawns a full pytest + private daemon
def test_stuck_daemon_fails_the_test_in_seconds(tmp_path: Path) -> None:
    """Suspended daemon + blocking store op -> stack dump + failure within the bound."""
    probe_out = tmp_path / "probe.json"
    with inner_runtime_parent() as runtime_parent:
        try:
            completed = run_inner_pytest(
                runtime_parent,
                [str(PROBE), "-q", "-p", "no:xdist"],
                env={
                    "CLIO_STUCK_PROBE_OUT": str(probe_out),
                    "CLIO_TEST_CTE_CALL_BOUND_S": str(CALL_BOUND_S),
                },
                backstop_s=INNER_RUN_BACKSTOP_S,
            )
            ended_at = time.time()
        finally:
            _resume_suspended_daemon(probe_out)
    output = completed.stdout + completed.stderr

    assert probe_out.exists(), f"the probe never suspended its daemon:\n{output}"
    suspended_at = float(json.loads(probe_out.read_text(encoding="utf-8"))["suspended_at"])
    stuck_for = ended_at - suspended_at

    assert completed.returncode != 0, f"a stuck daemon did not fail the test:\n{output}"
    assert stuck_for < CALL_BOUND_S + EXIT_SLACK_S, (
        f"the stuck store op held the test for {stuck_for:.1f}s (bound {CALL_BOUND_S}s):\n{output}"
    )
    assert stuck_for >= CALL_BOUND_S - 0.5, f"ended before the bound ({stuck_for:.1f}s)"
    # faulthandler's dump names the stuck frames: the probe test and the store op.
    assert "most recent call first" in output, f"no thread stack dump:\n{output}"
    assert "_stuck_daemon_probe.py" in output, output
    assert "storage.py" in output, output
