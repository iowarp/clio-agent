"""What a plugin page's open is doing, step by step, for a client to show while it waits.

Opening a plugin page can take a while the first time: CLIO prepares the plugin's
environment (provisioning, such as a ``uv`` build), starts the plugin's MCP server, then
reads the page's resource. Each page keeps one startup record, served by
``GET /v1/plugin-pages/{plugin}/{page}/status``::

    {"state": "idle" | "starting" | "ready" | "failed",
     "steps": [{"id": "env" | "server" | "page",
                "state": "pending" | "active" | "done" | "failed",
                "elapsed_ms": int}],
     "error": str}  # only when failed

The open path (:meth:`~clio_agent.gact.plugin_pages.PluginPages.open`) moves it along.
While the server is being prepared, the split between ``env`` and ``server`` follows
the server's own preparation phase (:mod:`clio_agent.tools.mcp_prepare`). A page never
opened is ``idle`` with every step ``pending``. Nothing here names a particular plugin.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

STEP_ENV = "env"
STEP_SERVER = "server"
STEP_PAGE = "page"
STEPS = (STEP_ENV, STEP_SERVER, STEP_PAGE)

IDLE = "idle"
STARTING = "starting"
READY = "ready"
FAILED = "failed"

PENDING = "pending"
ACTIVE = "active"
DONE = "done"

#: The longest error kept on a record.
MAX_ERROR = 2000


@dataclass
class _Step:
    state: str = PENDING
    started: float | None = None
    ended: float | None = None

    def begin(self, now: float) -> None:
        if self.state == PENDING:
            self.state, self.started = ACTIVE, now

    def finish(self, now: float, state: str = DONE) -> None:
        if self.state in (DONE, FAILED):
            return
        if self.started is None:
            self.started = now
        self.state, self.ended = state, now

    def elapsed_ms(self, now: float) -> int:
        if self.started is None:
            return 0
        return max(0, int(((self.ended if self.ended is not None else now) - self.started) * 1000))


@dataclass
class _Startup:
    state: str = STARTING
    context: str = ""
    namespace: str = ""
    error: str = ""
    steps: dict[str, _Step] = field(default_factory=lambda: {step: _Step() for step in STEPS})


class PageStartups:
    """The startup record of every page this process opened."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], _Startup] = {}

    def begin(self, plugin: str, page: str) -> None:
        """A new open of the page: its environment is being prepared.

        An open already in progress keeps its record; this one joins it.
        """

        now = time.monotonic()
        with self._lock:
            current = self._records.get((plugin, page))
            if current is not None and current.state == STARTING:
                return
            record = _Startup()
            record.steps[STEP_ENV].begin(now)
            self._records[(plugin, page)] = record

    def attach(self, plugin: str, page: str, *, context: str = "", namespace: str = "") -> None:
        """What the open learned: the page's server, then its context session.

        Once the context is known, the tab read that follows in it is this open's.
        """

        with self._lock:
            record = self._records.get((plugin, page))
            if record is not None and record.state == STARTING:
                record.context = context or record.context
                record.namespace = namespace or record.namespace

    def server_ready(self, session_id: str, plugin: str, page: str) -> None:
        """The page's server is up: the environment and server steps are done."""

        now = time.monotonic()
        with self._lock:
            record = self._records.get((plugin, page))
            if record is None or record.state != STARTING or record.context != session_id:
                return
            record.steps[STEP_ENV].finish(now)
            record.steps[STEP_SERVER].finish(now)
            record.steps[STEP_PAGE].begin(now)

    def ready(self, plugin: str, page: str) -> None:
        """The page's resource was read: the open is done."""

        now = time.monotonic()
        with self._lock:
            record = self._records.get((plugin, page))
            if record is None or record.state != STARTING:
                return
            for step in STEPS:
                record.steps[step].finish(now)
            record.state = READY

    def fail(self, plugin: str, page: str, error: str) -> None:
        """The open failed: the step in progress (or the first unfinished one) failed."""

        now = time.monotonic()
        with self._lock:
            record = self._records.get((plugin, page))
            if record is None:
                record = _Startup()
                self._records[(plugin, page)] = record
            if record.state != STARTING:
                return
            self._follow_preparation(record, now)
            unfinished = [
                record.steps[step]
                for step in STEPS
                if record.steps[step].state in (PENDING, ACTIVE)
            ]
            active = [step for step in unfinished if step.state == ACTIVE]
            if failing := (active or unfinished)[:1]:
                failing[0].finish(now, FAILED)
            record.state = FAILED
            record.error = (error or "the page did not open")[:MAX_ERROR]

    def status(self, plugin: str, page: str) -> dict[str, Any]:
        """The page's startup record as the status route sends it."""

        now = time.monotonic()
        with self._lock:
            record = self._records.get((plugin, page))
            if record is None:
                return {
                    "state": IDLE,
                    "steps": [{"id": step, "state": PENDING, "elapsed_ms": 0} for step in STEPS],
                }
            if record.state == STARTING:
                self._follow_preparation(record, now)
            row: dict[str, Any] = {
                "state": record.state,
                "steps": [
                    {
                        "id": step,
                        "state": record.steps[step].state,
                        "elapsed_ms": record.steps[step].elapsed_ms(now),
                    }
                    for step in STEPS
                ],
            }
            if record.state == FAILED:
                row["error"] = record.error
            return row

    @staticmethod
    def _follow_preparation(record: _Startup, now: float) -> None:
        """Split ``env`` from ``server`` by the server's own preparation phase."""

        from clio_agent.tools import mcp_prepare  # noqa: PLC0415

        env, server = record.steps[STEP_ENV], record.steps[STEP_SERVER]
        if not record.namespace or env.state != ACTIVE:
            return
        row = mcp_prepare.preparation_state(record.namespace) or {}
        phase = str(row.get("phase") or "")
        if row.get("state") == mcp_prepare.PREPARING and phase in (
            mcp_prepare.PHASE_LAUNCH,
            mcp_prepare.PHASE_CONNECT,
        ):
            env.finish(now)
            server.begin(now)

    def forget(self, plugin: str, page: str) -> None:
        with self._lock:
            self._records.pop((plugin, page), None)


def page_startups(app: Any) -> PageStartups:
    """The app's page startup records, created on first use."""

    startups = getattr(app.state, "plugin_page_startups", None)
    if not isinstance(startups, PageStartups):
        startups = PageStartups()
        app.state.plugin_page_startups = startups
    return startups


__all__ = [
    "FAILED",
    "IDLE",
    "READY",
    "STARTING",
    "STEPS",
    "STEP_ENV",
    "STEP_PAGE",
    "STEP_SERVER",
    "PageStartups",
    "page_startups",
]
