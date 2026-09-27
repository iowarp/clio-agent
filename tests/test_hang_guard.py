"""The per-test limit ends every hang with a failed test and a stack dump.

Runs ``_hang_guard_probe.py`` in a nested pytest session under xdist (``-n 1``), the
way CI runs the suite; the two hanging tests carry a 3 s limit. The probe hangs twice: once parked in Python
and once inside a native call that holds the GIL (the clio-core binding's shape,
which no Python timer can interrupt). Each hang must fail its own test, carry every
thread's stack into the controller's report (a stock pytest-timeout thread dump dies
with the xdist worker), and leave the session running: the tests after each hang
still pass.
"""

from __future__ import annotations

import re

import pytest

from tests._cte_isolation import cte_isolation_available
from tests._hang_guard_probe import HANG_LIMIT_S
from tests._inner_suite import REPO_ROOT, inner_runtime_parent, run_inner_pytest

PROBE = REPO_ROOT / "tests" / "_hang_guard_probe.py"
# Backstop only: two worker restarts, each re-importing the suite conftest and
# booting a private clio-core daemon, plus the two limits. A broken guard hangs
# forever and is killed here.
INNER_RUN_BACKSTOP_S = 240.0


@pytest.mark.timeout(INNER_RUN_BACKSTOP_S + 60)  # runs a nested xdist session
def test_both_hang_shapes_fail_their_test_with_a_dump_and_the_run_continues() -> None:
    with inner_runtime_parent() as runtime_parent:
        completed = run_inner_pytest(
            runtime_parent,
            [str(PROBE), "-n", "1", "-rA"],
            backstop_s=INNER_RUN_BACKSTOP_S,
        )
    output = completed.stdout + completed.stderr

    assert completed.returncode == 1, output  # tests failed; not a crash of the run
    assert re.search(r"\b2 failed, 2 passed\b", output), output
    assert "PASSED tests/_hang_guard_probe.py::test_before_the_hangs" in output, output
    assert "PASSED tests/_hang_guard_probe.py::test_after_the_hangs" in output, output

    python_hang = _failure_section(output, "test_hangs_in_python")
    assert f"exceeded its {HANG_LIMIT_S}s per-test limit" in python_hang, python_hang
    assert "most recent call first" in python_hang, python_hang
    assert "in test_hangs_in_python" in python_hang, python_hang

    gil_hang = _failure_section(output, "test_hangs_holding_the_gil")
    assert "while the GIL was held" in gil_hang, gil_hang
    assert "most recent call first" in gil_hang, gil_hang
    assert "in test_hangs_holding_the_gil" in gil_hang, gil_hang

    if cte_isolation_available():
        # The crashed worker's private daemon is described next to its stacks.
        for section in (python_hang, gil_hang):
            assert "private clio-core daemon of worker pid" in section, section
            assert "clio-runtime.log" in section, section


def _failure_section(output: str, test_name: str) -> str:
    """The FAILURES block xdist printed for the worker that crashed in ``test_name``."""
    marker = f"crashed while running 'tests/_hang_guard_probe.py::{test_name}'"
    start = output.find(marker)
    assert start != -1, f"no crash report for {test_name}:\n{output}"
    following = [output.find(m, start + 1) for m in ("\n____", "\n====")]
    end = min((i for i in following if i != -1), default=len(output))
    return output[start:end]
