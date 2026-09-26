"""Per-test hard limit that no hang can hide from (pytest-timeout, GIL-independent).

WHY. pytest-timeout owns the per-test limit (``timeout`` in ``pyproject.toml``, the
``@pytest.mark.timeout(N)`` marker, ``--timeout`` on the command line), and its
``thread`` method is the right shape: dump every thread's stack, then end the process
so the run cannot sit until GitHub's 6-hour job limit. Its stock ``thread`` timer has
two gaps this suite hits:

* **A native call holding the GIL defeats it.** The stock timer is a Python
  ``threading.Timer``, and a Python thread cannot run while a native call holds the
  GIL. The clio-core CTE binding holds the GIL for the whole of every blocking RPC
  (``Tag(...)``, ``GetBlobSize``, ``GetBlob``, ``PutBlob``, ``DelBlob``; PR #1473),
  so a stuck daemon hangs the interpreter AND the stock timer.
* **Under xdist the dump is lost.** The stock timer writes the stacks through the
  worker's terminal writer, which never reaches the controller; the run only says
  ``worker 'gwN' crashed while running <test>`` with no stack.

WHAT. :func:`pytest_timeout_set_timer` claims the timer (firstresult) and arms the
same ``thread`` semantics on a thread that needs no GIL: ``faulthandler``'s C watchdog
(``dump_traceback_later(exit=True)``) dumps every thread's stack and exits the
process. A Python timer fires first and writes a header naming the test (it cannot
run while the GIL is held; the C dump then still names the test's frame). The dump
goes to the real stderr in a plain run; in an xdist worker it goes to a per-worker
file the controller attaches to the crash report in :func:`pytest_handlecrashitem`,
so the stacks land in the FAILURES section of the CI log.

:func:`bounded_native_call` puts a tighter deadline on one native call (the clio-core
store ops, see ``tests/_cte_bounded.py``) on the same watchdog.

The limit therefore always ends in a failed test with a stack dump, never a silent
hang: under xdist the worker dies, xdist reports that test failed and replaces the
worker; in a single-process run the session ends at that test.
"""

from __future__ import annotations

import contextlib
import faulthandler
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

import pytest

from tests._crash_forensics import worker_daemon_report

HANG_DUMP_DIR_ENV = "CLIO_TEST_HANG_DUMP_DIR"

# Seconds between the Python header timer (at the limit) and the C watchdog's
# dump-and-exit. The header needs the GIL; the gap lets it print first when the GIL is
# free, and costs nothing when it is held (the C watchdog fires regardless).
_HEADER_LEAD_SECONDS = 1.0

_TIMEOUT_BANNER = f"{'+' * 20} Timeout {'+' * 20}"

_dump_stream: IO[str] | None = None

# The C watchdog is ONE process-global timer (``faulthandler`` keeps a single pending
# dump). Every armed deadline -- the running test's limit plus any open bounded native
# call, possibly on several threads -- lives here, and the timer always points at the
# earliest one.
_lock = threading.Lock()
_test_deadline: float | None = None
_call_deadlines: dict[int, float] = {}
_next_token = 0


def _rearm_locked() -> None:
    """Point the C watchdog at the earliest armed deadline (caller holds ``_lock``)."""
    deadlines = list(_call_deadlines.values())
    if _test_deadline is not None:
        deadlines.append(_test_deadline)
    if not deadlines or _dump_stream is None:
        faulthandler.cancel_dump_traceback_later()
        return
    remaining = max(min(deadlines) - time.monotonic(), 0.001)
    faulthandler.dump_traceback_later(remaining, exit=True, file=_dump_stream)


@contextlib.contextmanager
def bounded_native_call(seconds: float) -> Iterator[None]:
    """Hard-bound a native call that may hold the GIL (a stuck call ends the process).

    A call still running after ``seconds`` gets every thread's stack dumped (the
    frames name the caller and the test) and the process exits, failing the test in
    seconds instead of hanging it. Nests and works across threads: the earliest open
    deadline always wins, and closing one restores the next. Outside a configured
    pytest process (no dump stream) it is a no-op.

    Args:
        seconds: The hard bound for this call.
    """
    global _next_token
    with _lock:
        _next_token += 1
        token = _next_token
        _call_deadlines[token] = time.monotonic() + seconds
        _rearm_locked()
    try:
        yield
    finally:
        with _lock:
            _call_deadlines.pop(token, None)
            _rearm_locked()


def _worker_id(config: pytest.Config) -> str | None:
    """Return the xdist worker id (``gw0``...) or None in a controller/plain run."""
    workerinput = getattr(config, "workerinput", None)
    if workerinput is None:
        return None
    return str(workerinput["workerid"])


def configure(config: pytest.Config, dump_root: Path) -> None:
    """Open this process's dump stream; call from the suite's ``pytest_configure``.

    Must run while pytest's global capture is suspended (``pytest_configure`` is), so a
    plain run duplicates the REAL stderr, not a capture temp file.

    Args:
        config: The pytest config of this process.
        dump_root: Directory for per-worker dump files. The controller creates it and
            exports it to its workers through ``CLIO_TEST_HANG_DUMP_DIR``.
    """
    global _dump_stream
    if _dump_stream is not None:
        return
    worker_id = _worker_id(config)
    if worker_id is None:
        dump_root.mkdir(parents=True, exist_ok=True)
        os.environ[HANG_DUMP_DIR_ENV] = str(dump_root)
        _dump_stream = os.fdopen(os.dup(2), "w", encoding="utf-8", errors="replace")
        return
    worker_dir = Path(os.environ.get(HANG_DUMP_DIR_ENV) or dump_root)
    worker_dir.mkdir(parents=True, exist_ok=True)
    _dump_stream = open(  # noqa: SIM115 - held for the process lifetime (faulthandler target)
        worker_dir / f"{worker_id}-{os.getpid()}.log", "w", encoding="utf-8", errors="replace"
    )


def unconfigure() -> None:
    """Disarm the watchdog and close the dump stream (the suite's ``pytest_unconfigure``)."""
    global _dump_stream, _test_deadline
    with _lock:
        _call_deadlines.clear()
        _test_deadline = None
        faulthandler.cancel_dump_traceback_later()
    if _dump_stream is not None:
        _dump_stream.close()
        _dump_stream = None


@pytest.hookimpl(tryfirst=True, optionalhook=True)
def pytest_timeout_set_timer(item: pytest.Item, settings: Any) -> bool | None:
    """Arm the per-test limit on a GIL-independent watchdog (claims pytest-timeout's timer)."""
    global _test_deadline
    stream = _dump_stream
    if stream is None or settings.method != "thread":
        return None  # signal method, or not configured: pytest-timeout's own timer
    limit = float(settings.timeout)
    started = time.monotonic()

    fired = threading.Event()

    def _header() -> None:
        fired.set()
        elapsed = time.monotonic() - started
        stream.write(
            f"\n{_TIMEOUT_BANNER}\n"
            f"{item.nodeid} exceeded its {limit:g}s per-test limit (running {elapsed:.1f}s).\n"
            "Every thread's stack follows; the process then exits so the run cannot hang.\n"
        )
        stream.flush()

    header = threading.Timer(limit, _header)
    header.daemon = True
    header.name = f"clio-hang-guard {item.nodeid}"
    header.start()
    with _lock:
        _test_deadline = started + limit + _HEADER_LEAD_SECONDS
        _rearm_locked()

    def _cancel() -> None:
        global _test_deadline
        header.cancel()
        with _lock:
            _test_deadline = None
            _rearm_locked()
            if fired.is_set() and stream.seekable():
                # Finished inside the lead window: drop the banner so a later crash in
                # this worker is not misreported as this test's timeout.
                stream.seek(0)
                stream.truncate()

    item.cancel_timeout = _cancel  # type: ignore[attr-defined]  # pytest-timeout's cancel slot
    return True


@pytest.hookimpl(optionalhook=True)
def pytest_handlecrashitem(crashitem: str, report: pytest.TestReport, sched: Any) -> None:
    """Attach a crashed worker's hang dump to its xdist crash report (controller side)."""
    del crashitem, sched
    dump_dir = os.environ.get(HANG_DUMP_DIR_ENV)
    node = getattr(report, "node", None)
    worker_id = getattr(getattr(node, "gateway", None), "id", None)
    if not dump_dir or not worker_id:
        return
    # One dump file per worker process (``<worker>-<pid>.log``), created empty at its
    # start; the newest is the process that just died.
    candidates = sorted(
        Path(dump_dir).glob(f"{worker_id}-*.log"), key=lambda path: path.stat().st_mtime
    )
    if not candidates:
        return
    latest = candidates[-1]
    worker_pid = int(latest.stem.rsplit("-", 1)[1])
    text = latest.read_text(encoding="utf-8", errors="replace")
    latest.rename(latest.with_suffix(".reported"))
    if not text:
        text = (
            f"(no hang dump from {worker_id}: the worker died without reaching a time "
            "limit, so this is a crash, not a timeout)"
        )
    elif _TIMEOUT_BANNER not in text:
        text = (
            f"{_TIMEOUT_BANNER}\nA hard time limit fired while the GIL was held (no Python "
            "thread could run), typically a native clio-core call past its bound "
            "(tests/_cte_bounded.py). Every thread's stack:\n" + text
        )
    daemon = worker_daemon_report(worker_pid)
    report.longrepr = f"{report.longrepr}\n{text}" + (f"\n{daemon}" if daemon else "")
