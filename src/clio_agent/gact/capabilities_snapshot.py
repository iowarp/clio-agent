"""Owner of the precomputed, I/O-backed inputs of ``GET /v1/capabilities``.

Clients probe capabilities to decide whether a workspace is reachable (gact-tui
gives it 3.5 s), so the answer must not depend on how busy the server is. Two of
its inputs touch the plugin store: the installed A2UI catalog ids (store
revision, records and catalog parsing; ~2.7 s on a cold process) and the default
marketplace pin in ``versions``. The route used to compute both per request on
the shared anyio worker pool, where under load it queued behind every other sync
route for seconds.

This module computes those inputs on one dedicated daemon thread and keeps the
latest snapshot. The thread starts with the app's lifespan (or on the first
request) and refreshes:

* every :data:`DEFAULT_REFRESH_S` seconds (``CLIO_GACT_CAPABILITIES_REFRESH_S``),
  which picks up a plugin install made by another process (the CLI);
* at once when a request sees the catalog registry's ``generation`` move past
  the snapshot's (an in-process plugin install/update/uninstall), and that
  request waits for the fresh snapshot so it never reports the old catalogs.

The route reads the snapshot on the event loop and builds the remaining,
in-memory fields live. Until the first snapshot exists (or when a refresh misses
:data:`INPUT_WAIT_S`), it answers with what it has and a typed, recoverable v3
degradation instead of stalling the probe.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from clio_agent.gact.protocol.v3 import utcnow_iso
from clio_agent.gact.types import VersionInfo
from clio_agent.gact.version_info import build_version_info

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

REFRESH_ENV = "CLIO_GACT_CAPABILITIES_REFRESH_S"
DEFAULT_REFRESH_S = 5.0
# Below gact-tui's 3.5 s probe budget: an honest degraded answer beats a timeout.
INPUT_WAIT_S = 3.0
STOP_JOIN_S = 2.0

INPUTS_PENDING = "capability_inputs_pending"
INPUTS_STALE = "capability_inputs_stale"

_CREATE_LOCK = threading.Lock()


@dataclass(frozen=True)
class CapabilitySnapshot:
    """One collected set of the I/O-backed capability inputs."""

    versions: VersionInfo
    a2ui_capabilities: dict[str, Any]
    generation: int
    observed_at: str
    compute_ms: float


@dataclass(frozen=True)
class ResolvedInputs:
    """What one capabilities response uses, with the v3 degradations it owes."""

    versions: VersionInfo
    a2ui_capabilities: dict[str, Any]
    degradations: list[dict[str, Any]] = field(default_factory=list)


def refresh_interval_s() -> float:
    """The periodic refresh interval, from ``CLIO_GACT_CAPABILITIES_REFRESH_S``."""

    raw = os.environ.get(REFRESH_ENV, "").strip()
    if not raw:
        return DEFAULT_REFRESH_S
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        logger.warning(
            "capabilities_refresh_interval_invalid value=%r default=%s", raw, DEFAULT_REFRESH_S
        )
        return DEFAULT_REFRESH_S
    return value


def _generation(app: "FastAPI | None") -> int:
    registry = getattr(getattr(app, "state", None), "a2ui_catalogs", None)
    return int(getattr(registry, "generation", 0) or 0)


def _resolve_future(future: "asyncio.Future[None]") -> None:
    if not future.done():
        future.set_result(None)


class CapabilityInputs:
    """The refresher thread and latest snapshot for one app."""

    def __init__(self, app: "FastAPI", *, refresh_s: float) -> None:
        self._app_ref = weakref.ref(app)
        self._refresh_s = refresh_s
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: CapabilitySnapshot | None = None
        self._failure: str | None = None
        self._waiters: list[tuple[int, asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

    @property
    def snapshot(self) -> CapabilitySnapshot | None:
        return self._snapshot

    @property
    def failure(self) -> str | None:
        """The reason the last refresh failed, or ``None`` after a good one."""

        return self._failure

    def start(self) -> None:
        with self._lock:
            if self._stop.is_set() or (self._thread is not None and self._thread.is_alive()):
                return
            self._thread = threading.Thread(
                target=self._run, name="gact-capabilities-refresh", daemon=True
            )
            self._thread.start()

    def kick(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = STOP_JOIN_S) -> bool:
        """Stop the refresher; ``True`` when its thread has ended within ``timeout``."""

        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._release_waiters(None)
        return thread is None or not thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            app = self._app_ref()
            if app is None:
                return
            again = self._refresh_once(app)
            del app
            if not again:
                self._wake.wait(self._refresh_s)

    def _refresh_once(self, app: "FastAPI") -> bool:
        """Collect one snapshot; ``True`` when the registry moved during the collection."""

        from clio_agent.gact.a2ui_capabilities import agent_capabilities  # noqa: PLC0415

        generation = _generation(app)
        started = time.perf_counter()
        try:
            versions = build_version_info()
            a2ui = agent_capabilities(app, None)
        except Exception as exc:  # noqa: BLE001 - typed on the response, retried next pass
            self._failure = f"{type(exc).__name__}: {exc}"
            logger.warning("capabilities_inputs_refresh_failed reason=%s", exc, exc_info=True)
            self._release_waiters(None)
            return False
        self._snapshot = CapabilitySnapshot(
            versions=versions,
            a2ui_capabilities=a2ui,
            generation=generation,
            observed_at=utcnow_iso(),
            compute_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        self._failure = None
        self._release_waiters(generation)
        # Discovery itself bumps the generation when another process changed the store.
        return _generation(app) != generation and not self._stop.is_set()

    def _release_waiters(self, upto: int | None) -> None:
        with self._lock:
            ready = [w for w in self._waiters if upto is None or w[0] <= upto]
            self._waiters = [w for w in self._waiters if not any(w is r for r in ready)]
        for _wanted, loop, future in ready:
            try:
                loop.call_soon_threadsafe(_resolve_future, future)
            except RuntimeError:  # the waiting loop already closed
                pass

    async def current(self, *, wait_s: float = INPUT_WAIT_S) -> CapabilitySnapshot | None:
        """The latest snapshot, waiting up to ``wait_s`` when it is missing or outdated."""

        self.start()
        wanted = _generation(self._app_ref())
        snapshot = self._snapshot
        if (snapshot is not None and snapshot.generation >= wanted) or self._stop.is_set():
            return snapshot
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        entry = (wanted, loop, future)
        with self._lock:
            self._waiters.append(entry)
        self.kick()
        try:
            snapshot = self._snapshot
            if snapshot is None or snapshot.generation < wanted:
                await asyncio.wait_for(future, wait_s)
        except TimeoutError:
            pass
        finally:
            with self._lock:
                self._waiters = [w for w in self._waiters if w is not entry]
        return self._snapshot


def inputs_for(app: "FastAPI") -> CapabilityInputs:
    """The app's :class:`CapabilityInputs`, created on first use."""

    inputs = getattr(app.state, "capability_inputs", None)
    if inputs is None:
        with _CREATE_LOCK:
            inputs = getattr(app.state, "capability_inputs", None)
            if inputs is None:
                inputs = CapabilityInputs(app, refresh_s=refresh_interval_s())
                app.state.capability_inputs = inputs
    return inputs


def start(app: "FastAPI") -> None:
    """Start collecting at boot, so the first probe already finds a snapshot."""

    inputs_for(app).start()


def stop(app: "FastAPI") -> None:
    """Stop the refresher (shutdown). Its thread is a daemon, so a slow pass cannot pin exit."""

    inputs = getattr(app.state, "capability_inputs", None)
    if inputs is not None and not inputs.stop():
        logger.warning(
            "capabilities_refresh_stop_pending reason=refresh_in_progress join_s=%s", STOP_JOIN_S
        )


async def resolve(app: "FastAPI") -> ResolvedInputs:
    """The inputs one capabilities response uses, never blocking past :data:`INPUT_WAIT_S`."""

    inputs = inputs_for(app)
    snapshot = await inputs.current()
    detail = f" Last collection failed: {inputs.failure}" if inputs.failure else ""
    if snapshot is None:
        from clio_agent.gact.a2ui_capabilities import (  # noqa: PLC0415
            builtin_agent_capabilities,
        )

        return ResolvedInputs(
            versions=build_version_info(include_marketplace=False),
            a2ui_capabilities=builtin_agent_capabilities(app),
            degradations=[
                {
                    "code": INPUTS_PENDING,
                    "reason": (
                        "Installed A2UI catalogs and the marketplace pin are still being "
                        "collected; only builtin catalogs are listed." + detail
                    ),
                    "capability": "a2ui_capabilities",
                    "recoverable": True,
                }
            ],
        )
    degradations: list[dict[str, Any]] = []
    if snapshot.generation < _generation(app):
        degradations.append(
            {
                "code": INPUTS_STALE,
                "reason": (
                    "Installed plugins changed and the catalog list is being refreshed; "
                    f"this list was observed at {snapshot.observed_at}." + detail
                ),
                "capability": "a2ui_capabilities",
                "recoverable": True,
            }
        )
    return ResolvedInputs(
        versions=snapshot.versions,
        a2ui_capabilities=snapshot.a2ui_capabilities,
        degradations=degradations,
    )


__all__ = [
    "DEFAULT_REFRESH_S",
    "INPUTS_PENDING",
    "INPUTS_STALE",
    "INPUT_WAIT_S",
    "REFRESH_ENV",
    "CapabilityInputs",
    "CapabilitySnapshot",
    "ResolvedInputs",
    "inputs_for",
    "refresh_interval_s",
    "resolve",
    "start",
    "stop",
]
