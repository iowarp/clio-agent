"""clio-core client attach: one config for daemon + client, and a typed attach state.

Owner module for the part of the connect-or-spawn lifecycle that runs *after* a
daemon is known to be listening: pointing this process's native client at the
right runtime, checking that the attach actually succeeded, and publishing the
attach progress so ``/v1/health`` can report it while it is still in flight.

SINGLE-SOURCE CONFIG. The native client (``clio_init(kClient, False)``) takes no
config-path argument: it learns the runtime's ports ONLY from ``$CLIO_SERVER_CONF``
(else ``~/.clio/clio.yaml``). The spawned daemon was always handed
``CLIO_SERVER_CONF=<config_path>``, but the attaching process never was, so a
daemon composed from a config whose ``networking.port`` differs from
``~/.clio/clio.yaml`` served one port while the client waited on another. That is
the Linux remote-deploy failure (live on ``ares``, 2026-09-25): the launcher's
per-install port (21045) went into the daemon's config; the client waited 30 s on
9413 (``WaitForLocalServer ... Cannot connect to local server``), twice.
:func:`export_client_config` closes that gap: the daemon, the native client, and
the Python liveness probe all read the same file.

TYPED ATTACH STATE. The attach runs off the server's event loop and can take a
while (a fresh daemon spawn, a native handshake). :class:`ClioCoreAttachState` is
a process-local record of where it is -- ``starting`` / ``attached`` /
``unavailable(reason)`` / ``not_selected`` -- that the doctor turns into the
``clio_core_attach`` row (:func:`clio_agent.runtime.clio_core_health.probe_clio_core_attach`).
It only reports what already happened; it never times anything out.

POST-ATTACH PROBE. A clean ``clio_init`` + ``initialize_cte`` does not prove the
binding works; :func:`verify_post_attach` runs one real RPC, bounded through the
async ``Future`` API (the blocking binding calls hold the GIL), before the store is
handed out, so a half-attached client degrades typed at init instead of hanging.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from clio_agent.arc.init_degradation import (
    CLIO_CORE_CLIENT_ATTACH_FAILED,
    CLIO_CORE_POST_ATTACH_PROBE_TIMEOUT,
)

if TYPE_CHECKING:
    from clio_agent.arc.storage import ARCStore, ClioCoreStore

logger = logging.getLogger(__name__)

# How often the post-attach probe checks its Future. ``Future.done()`` returns at once
# whatever the daemon's state, and the sleep between checks releases the GIL.
# ``Future.wait(max_sec)`` is NOT a bound: against a daemon that is gone it blocks
# indefinitely (holding the GIL), which is how a daemon that died at startup hung CI.
_PROBE_POLL_S = 0.02


class ClioCoreAttachPhase(str, Enum):
    """Where this process's clio-core attach is."""

    IDLE = "idle"  # no ARC store has been built in this process yet
    STARTING = "starting"  # connect-or-spawn + native attach in flight
    ATTACHED = "attached"  # the clio-core store is live
    UNAVAILABLE = "unavailable"  # attach failed; ARC degraded to LocalFS (reason says why)
    NOT_SELECTED = "not_selected"  # CLIO_ARC_STORE=local chose LocalFS deliberately


@dataclass(frozen=True)
class ClioCoreAttachState:
    """A snapshot of this process's clio-core attach progress.

    Attributes:
        phase: The current :class:`ClioCoreAttachPhase`.
        reason: Typed reason code for the phase (e.g. ``clio_core_starting``, or the
            init-degrade reason when ``unavailable``).
        config_path: The clio-core config the daemon and the client both use.
        port: The RPC port that config declares (``None`` before resolution).
        error: The failure message when ``unavailable``, else empty.
        since: ``time.time()`` when the phase was entered.
    """

    phase: ClioCoreAttachPhase
    reason: str
    config_path: str = ""
    port: int | None = None
    error: str = ""
    since: float = 0.0

    def to_details(self) -> dict[str, object]:
        """JSON-safe detail payload for the doctor row."""
        return {
            "phase": self.phase.value,
            "reason": self.reason,
            "config_path": self.config_path,
            "port": self.port,
            "error": self.error,
            "since": self.since,
        }


_IDLE = ClioCoreAttachState(phase=ClioCoreAttachPhase.IDLE, reason="clio_core_not_started")
_lock = threading.Lock()
_state: ClioCoreAttachState = _IDLE


def _set(state: ClioCoreAttachState) -> None:
    global _state
    with _lock:
        _state = replace(state, since=time.time())


def mark_starting(config_path: str, port: int | None) -> None:
    """Record that connect-or-spawn + attach against ``config_path`` has begun."""
    _set(ClioCoreAttachState(ClioCoreAttachPhase.STARTING, "clio_core_starting", config_path, port))


def mark_attached(config_path: str, port: int | None) -> None:
    """Record a live clio-core store."""
    _set(ClioCoreAttachState(ClioCoreAttachPhase.ATTACHED, "clio_core_attached", config_path, port))


def mark_unavailable(reason: str, error: str, config_path: str, port: int | None) -> None:
    """Record a failed attach (ARC degraded to LocalFS) with its typed ``reason``."""
    _set(ClioCoreAttachState(ClioCoreAttachPhase.UNAVAILABLE, reason, config_path, port, error))


def mark_not_selected() -> None:
    """Record that LocalFS was chosen deliberately (no clio-core attach attempted)."""
    _set(ClioCoreAttachState(ClioCoreAttachPhase.NOT_SELECTED, "clio_core_not_selected"))


def attach_state_snapshot() -> ClioCoreAttachState:
    """Return this process's current attach state."""
    with _lock:
        return _state


def reset_attach_state() -> None:
    """Return to ``idle`` (test seam; production never resets)."""
    global _state
    with _lock:
        _state = _IDLE


class ClioCoreAttachError(RuntimeError):
    """The native clio-core client could not attach to a daemon that IS listening.

    Carries ``degradation_reason`` so the init-degrade classifier records
    ``clio_core_client_attach_failed`` rather than a generic init error, and names
    the port and config the attach used so the operator can see a mismatch at once.
    """

    degradation_reason = CLIO_CORE_CLIENT_ATTACH_FAILED

    def __init__(
        self,
        *,
        port: int,
        config_path: str,
        stage: str = "client_init",
        detail: str = "",
        reason: str = CLIO_CORE_CLIENT_ATTACH_FAILED,
    ) -> None:
        self.degradation_reason = reason
        self.port = port
        self.config_path = config_path
        self.stage = stage
        what = detail or (
            "the native client handshake did not complete"
            if stage == "client_init"
            else "the first RPC after the attach did not answer"
        )
        super().__init__(
            f"clio-core client attach failed at stage={stage}: a daemon is listening on port "
            f"{port}, but {what} (config: {config_path or '<none>'}, "
            f"CLIO_SERVER_CONF={os.environ.get('CLIO_SERVER_CONF', '') or '<unset>'})."
        )


def build_tracked_store(cfg: str, *, backend: str | None, data_dir: "str | Path") -> "ARCStore":
    """Build the clio-core ARC store for ``cfg``, publishing the attach state as it goes.

    ``starting`` before connect-or-spawn, ``attached`` on success, and on ANY init
    failure the LOUD degrade to LocalFS (#897: typed reason + WARNING + doctor row)
    plus ``unavailable(reason)``. The body of ``make_arc_store``'s ``cte`` branch.

    Args:
        cfg: The clio-core config path (daemon and client both use it).
        backend: The explicit ``backend`` arg given to ``make_arc_store``, if any.
        data_dir: The LocalFS directory to degrade to.

    Returns:
        A live ``ClioCoreStore``, or a ``LocalFSStore`` after a recorded degrade.
    """
    from clio_agent.arc import clio_core_file_capacity, storage  # noqa: PLC0415 - cycle
    from clio_agent.arc.init_degradation import record_arc_init_degradation  # noqa: PLC0415

    port = storage._resolve_runtime_port(cfg)
    mark_starting(cfg, port)
    try:
        clio_core_file_capacity.preflight_clio_core_config(cfg, env=os.environ)
        store = storage.ClioCoreStore(config_path=cfg)
    except Exception as exc:  # noqa: BLE001 - LOUD degrade to LocalFS, recorded below
        record = record_arc_init_degradation(
            backend=backend, config_path=cfg, error=exc, data_dir=str(data_dir)
        )
        mark_unavailable(record.reason, str(exc), cfg, port)
        return storage.LocalFSStore(data_dir)
    # The daemon's config, when this process adopted it (first config wins).
    effective = getattr(store, "_config_path", "") or cfg
    mark_attached(effective, storage._resolve_runtime_port(effective))
    return store


def export_client_config(config_path: str) -> None:
    """Point this process's native client at ``config_path`` (the daemon's config).

    ``clio_init`` reads its ports only from ``$CLIO_SERVER_CONF``; the daemon is
    spawned with that same variable, so exporting it here makes the daemon, the
    client, and the liveness probe read ONE file. One clio-core client per process,
    so a process-wide export is exact.
    """
    if config_path:
        os.environ["CLIO_SERVER_CONF"] = config_path


def attach_native_client(
    cte: object,
    *,
    config_path: str,
    port: int,
    on_failure: Callable[[], None],
) -> None:
    """Attach as a pure client (``clio_init(kClient, False)``) and CHECK the result.

    The binding returns ``False`` after its own wait for the runtime expires; that
    used to be ignored, so the next native call (``initialize_cte``) re-ran the whole
    client init and waited a second time before anything failed. Now a failed attach
    runs ``on_failure`` (client deregistration, so a process that never attached holds
    no vote in the shared daemon's last-one-out refcount) and raises
    :class:`ClioCoreAttachError` at once.

    Raises:
        ClioCoreAttachError: If the native client init reports failure.
    """
    client_init = getattr(cte, "clio_init", None) or cte.chimaera_init  # type: ignore[attr-defined]
    mode = (getattr(cte, "RuntimeMode", None) or cte.ChimaeraMode).kClient  # type: ignore[attr-defined]
    if client_init(mode, False):
        return
    on_failure()
    error = ClioCoreAttachError(port=port, config_path=config_path)
    logger.error("%s reason=%s", error, CLIO_CORE_CLIENT_ATTACH_FAILED)
    raise error


def verify_post_attach(store: "ClioCoreStore", *, on_failure: Callable[[], None]) -> None:
    """Prove a freshly attached store answers ONE real RPC, within a bound, before handing it out.

    A clean ``clio_init`` + ``initialize_cte`` does not prove the binding works; the
    first RPC does. The probe MUST be bounded: the binding's blocking calls
    (``Tag(...)``, ``GetBlobSize``, ``GetBlob``, ...) hold the GIL for the whole RPC, so a
    daemon that never answers freezes the entire interpreter, stall watchers included.
    So the probe uses the ASYNC API -- ``AsyncTagQuery`` on the store's liveness
    sentinel (a pure RPC: no tag is created, no ``Tag`` constructor runs) -- and polls
    its ``Future.done()`` until the bound expires, sleeping (GIL released) in between.
    ``Future.wait(max_sec)`` is only called once the Future is done: against a daemon
    that is GONE it ignores ``max_sec`` and blocks for good. The bound is the configured
    health-probe window (:func:`~clio_agent.arc.rpc_liveness.health_probe_window_s`). An
    expired bound (typed ``clio_core_post_attach_probe_timeout``) or a non-zero return
    code (``clio_core_client_attach_failed``) runs ``on_failure`` (client
    deregistration) and raises typed, so ARC degrades loudly at init instead of hanging.

    Raises:
        ClioCoreAttachError: ``stage="post_attach_probe"`` when the probe fails.
    """
    from clio_agent.arc.rpc_liveness import health_probe_window_s  # noqa: PLC0415 - cycle

    window = health_probe_window_s()
    future = store._client.AsyncTagQuery(
        store._HEALTH_PROBE_NAME, 1, store._cte.PoolQuery.Dynamic()
    )
    deadline = time.monotonic() + window
    reason = CLIO_CORE_CLIENT_ATTACH_FAILED
    while not future.done():
        if time.monotonic() >= deadline:
            detail = f"the first RPC after the attach did not answer within {window:g}s"
            reason = CLIO_CORE_POST_ATTACH_PROBE_TIMEOUT
            break
        time.sleep(_PROBE_POLL_S)
    else:
        code = future.wait(0)  # finished: returns its code at once
        if code == 0:
            return
        detail = f"the first RPC after the attach answered with return code {code}"
    on_failure()
    error = ClioCoreAttachError(
        port=store._gate.port,
        config_path=store._config_path,
        stage="post_attach_probe",
        detail=detail,
        reason=reason,
    )
    logger.error("%s reason=%s", error, reason)
    raise error
