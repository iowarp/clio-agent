"""One shutdown budget for a serving CLIO process (owner module, #1401).

Every write is committed to clio-core before it is acknowledged, so a stop needs no
drain to be correct: the graceful steps (cancel turns and tasks, settle native work,
release clio-core, close MCP servers) make it tidy, not safe. Work still running when
the budget runs out is recovered at the next start, exactly as after a crash.

So a stop is bounded. When the lifespan begins shutting down, :func:`begin` arms a
watchdog. If the process has not exited ``budget`` seconds later (a turn that ignores
cancellation, a native settlement waiting on a stuck process, an executor join at
interpreter exit), the watchdog logs ``reason=shutdown_budget_exceeded`` with every
thread's stack and ends the process with :data:`EXIT_BUDGET_EXCEEDED`.

The default budget sits under the 30 seconds that the desktop supervisor, the sidecar
launcher and ``clio stop`` give a server before forcing it, so the server always ends
itself, with a reason, before anything kills it. ``CLIO_SHUTDOWN_BUDGET_S`` overrides it.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_S = 20.0
#: EX_TEMPFAIL: the stop ended early; the next start recovers what was still running.
EXIT_BUDGET_EXCEEDED = 75


def budget_seconds(env: dict[str, str] | None = None) -> float:
    """The configured budget, or the default when unset or not a positive number."""

    raw = (env if env is not None else os.environ).get("CLIO_SHUTDOWN_BUDGET_S", "").strip()
    if not raw:
        return DEFAULT_BUDGET_S
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        logger.warning(
            "shutdown budget setting ignored reason=shutdown_budget_invalid value=%r default=%s",
            raw,
            DEFAULT_BUDGET_S,
        )
        return DEFAULT_BUDGET_S
    return value


def begin(app: Any, *, exit_process: Any = os._exit) -> threading.Event | None:
    """Arm the watchdog for a serving process; idempotent. Returns its cancel event.

    Only a real server (``app.state.server_boot``) is bounded: an embedded or test app
    shares its process with its host, which this must never end.
    """

    state = app.state
    if not getattr(state, "server_boot", False):
        return None
    armed: threading.Event | None = getattr(state, "shutdown_watchdog", None)
    if armed is not None:
        return armed
    done = threading.Event()
    state.shutdown_watchdog = done
    budget = budget_seconds()

    def watch() -> None:
        if done.wait(budget):
            return
        runner = getattr(state, "turn_runner", None)
        logger.error(
            "shutdown did not finish within its budget reason=shutdown_budget_exceeded "
            "budget_s=%s running_turns=%d; ending the process, the next start recovers "
            "unfinished work",
            budget,
            len(getattr(runner, "_turns", ()) or ()),
        )
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        logging.shutdown()  # flush the reason before the process ends
        exit_process(EXIT_BUDGET_EXCEEDED)

    threading.Thread(target=watch, name="clio-shutdown-budget", daemon=True).start()
    logger.info("shutdown started budget_s=%s", budget)
    return done
