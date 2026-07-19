"""clio-core daemon liveness gate + quarantine (owner module, #892).

Why this module exists
----------------------
clio-core's chimaera runtime is a host-global daemon; a clio-core client attaches to it
once and then issues native ``GetBlob``/``PutBlob``/``DelBlob`` calls. When that
daemon **dies while a client still holds an initialized binding**, the next native
op does not raise — it segfaults the *host* process with an access violation
(``0xC0000005``): no Python exception, no traceback, no recovery. For the gact
server a daemon crash therefore takes down the whole backend; under pytest it kills
the run. Every historical AV we have logged correlates with a dead runtime
(diagnosed 2026-07-11); the upstream fix — bindings that raise instead of AV — is
tracked as iowarp/clio-core#722.

The defense clio-agent ships now
--------------------------------
A cheap **liveness gate** wrapped around every clio-core op (:class:`LivenessGate`):

* Before an op reaches the native binding, the gate confirms the daemon is
  accepting connections. The probe result is cached for a short TTL
  (:data:`_DEFAULT_LIVENESS_TTL_S`, configurable) so the per-op overhead is a
  single ``time.monotonic()`` comparison in the common case, not a socket connect.
* On probe failure the store enters a **QUARANTINED** state and every op raises a
  typed :class:`ClioCoreRuntimeLostError` *before* touching the binding. This shrinks the
  AV window from "any time the daemon can die" to "the daemon dies within the TTL
  race" — a residual honestly owned here and closed upstream by #722.
* On the next op after quarantine the gate makes **one** guarded reconnect attempt
  (rate-limited to once per TTL) through the injected reconnect seam — the existing
  connect-or-spawn path in :mod:`clio_agent.arc.storage`, which spawns + rebinds and
  itself fails loud if a fresh daemon never binds the port (clio-core#725: a stale
  pidfile is overwritten by that spawn, so we do not hand-delete runtime artifacts).
  Success leaves quarantine (a typed INFO audit record); failure stays quarantined
  and re-raises the typed error.

This is a **fail-loud** surface, not a silent LocalFS fallback: backend selection
stays the deliberate choice documented in :func:`clio_agent.arc.storage.make_arc_store`.
The quarantine turns an un-catchable process crash into a typed, attributable,
trace-visible error, and — because gates register in a process-local registry —
into a doctor row (:func:`clio_agent.runtime.clio_core_health.probe_clio_core_liveness`).

The daemon port-resolution + socket-liveness helpers (``_resolve_runtime_port`` /
``_runtime_alive`` / ``_read_yaml_port`` and the default port) live here — this is
the liveness owner module — and are re-exported from ``storage`` for the callers and
tests that reach them there.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import weakref
from pathlib import Path
from typing import Callable, Optional

from clio_agent.errors import ClioError

logger = logging.getLogger(__name__)

_DEFAULT_RUNTIME_PORT = 9413

# Default liveness-probe cache TTL, in seconds. A probe result is trusted for this
# long so the per-op cost is one monotonic comparison, not a socket connect; the
# window is also the residual AV race (a daemon that dies mid-TTL is not yet seen).
# Small enough that a dead daemon is caught within a couple of seconds, large enough
# that a hot ReAct loop does not open a socket on every blob op. Configurable via
# ``arc.clio_core.liveness_ttl_s`` / env ``CLIO_ARC_CLIO_CORE_LIVENESS_TTL_S``.
_DEFAULT_LIVENESS_TTL_S = 3.0


# --------------------------------------------------------------------------- #
# daemon port resolution + socket liveness (moved here from storage.py, #892)
# --------------------------------------------------------------------------- #


def _read_yaml_port(path: str) -> Optional[int]:
    """Return ``networking.port`` from a clio-core YAML config, or None if absent."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        import yaml  # noqa: PLC0415

        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a parse miss falls through to the default port
        return None
    if isinstance(data, dict):
        net = data.get("networking")
        if isinstance(net, dict) and isinstance(net.get("port"), int):
            return int(net["port"])
    return None


def _resolve_runtime_port(config_path: str) -> int:
    """Resolve the chimaera RPC port so liveness probes match what the daemon binds.

    Honours the ``CLIO_CORE_PORT`` override, then mirrors clio-core's config lookup
    order (``$CLIO_SERVER_CONF`` / ``$CHI_SERVER_CONF``, the passed ``config_path``,
    ``~/.clio/clio.yaml``), defaulting to :data:`_DEFAULT_RUNTIME_PORT`.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    override = conf.resolve(
        "arc.core_port", env="CLIO_CORE_PORT", default="", cast=conf.as_str
    ).strip()
    if override:
        try:
            return int(override)
        except ValueError:
            logger.warning("ignoring non-integer CLIO_CORE_PORT=%r", override)
    candidates = [
        conf.resolve(
            "arc.server_conf", env="CLIO_SERVER_CONF", default="", cast=conf.as_str
        ).strip(),
        os.environ.get("CHI_SERVER_CONF", "").strip(),
        config_path,
        str(Path.home() / ".clio" / "clio.yaml"),
    ]
    for cand in candidates:
        if cand:
            port = _read_yaml_port(cand)
            if port is not None:
                return port
    return _DEFAULT_RUNTIME_PORT


def _runtime_alive(port: int) -> bool:
    """True if a clio-core runtime is accepting connections on ``127.0.0.1:port``."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _resolve_liveness_ttl_s() -> float:
    """Resolve the configured liveness-probe cache TTL (seconds), fail-safe to default.

    Reads ``arc.clio_core.liveness_ttl_s`` / env ``CLIO_ARC_CLIO_CORE_LIVENESS_TTL_S``; a negative
    or unparseable value falls back to :data:`_DEFAULT_LIVENESS_TTL_S` (a bad TTL must
    not silently disable the gate, which a 0/negative cache would encourage).
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    try:
        ttl = conf.resolve(
            "arc.clio_core.liveness_ttl_s",
            env="CLIO_ARC_CLIO_CORE_LIVENESS_TTL_S",
            default=_DEFAULT_LIVENESS_TTL_S,
            cast=conf.as_float,
        )
    except Exception:  # noqa: BLE001 - a malformed value must not disable the gate
        return _DEFAULT_LIVENESS_TTL_S
    return ttl if ttl >= 0 else _DEFAULT_LIVENESS_TTL_S


# --------------------------------------------------------------------------- #
# typed error + liveness gate
# --------------------------------------------------------------------------- #


class ClioCoreRuntimeLostError(ClioError):
    """The shared clio-core runtime is gone; the clio-core store is quarantined.

    A :class:`~clio_agent.errors.ClioError` so it serializes to a structured,
    attributable payload (``error_type='arc_runtime_lost'``) that reaches the trace
    instead of an un-catchable native access violation. Raised by the liveness gate
    *before* any native clio-core op when the daemon is not listening or a reconnect
    attempt failed — never a silent degradation and never a LocalFS fallback.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = "clio_core_daemon_not_listening",
        port: int | None = None,
        details: dict | None = None,
    ) -> None:
        merged: dict = {
            "reason": reason,
            "recovery_actions": [
                "restart_clio_core_daemon",
                "run_clio_doctor",
                "set_clio_arc_store_local",
                "retry",
            ],
        }
        if port is not None:
            merged["port"] = port
        if details:
            merged.update(details)
        super().__init__(message, error_type="arc_runtime_lost", details=merged)


class LivenessGate:
    """TTL-cached daemon-liveness gate with quarantine + one-shot guarded reconnect.

    Wrap every native clio-core op in :meth:`ensure_live`. It confirms the daemon is
    listening (cached for ``ttl_s``), quarantines the store and raises
    :class:`ClioCoreRuntimeLostError` on loss, and — once quarantined — makes at most one
    reconnect attempt per TTL through the caller-supplied ``reconnect`` seam. All
    state transitions are serialized by an :class:`threading.RLock` so concurrent
    turns cannot race the quarantine flag.

    Args:
        config_path: The clio-core config path (used to resolve the RPC port).
        log_level: The runtime log level, carried for the reconnect seam.
        probe: Injectable liveness probe ``(port) -> bool`` (defaults to a real TCP
            connect); the seam the tests drive to simulate daemon loss without an AV.
        ttl_s: Probe-cache TTL in seconds; ``None`` resolves the configured default.
    """

    def __init__(
        self,
        *,
        config_path: str = "",
        log_level: str = "error",
        probe: Callable[[int], bool] | None = None,
        ttl_s: float | None = None,
    ) -> None:
        self._config_path = config_path
        self._log_level = log_level
        self._probe = probe or _runtime_alive
        self._port = _resolve_runtime_port(config_path)
        self._ttl_s = _resolve_liveness_ttl_s() if ttl_s is None else float(ttl_s)
        self._lock = threading.RLock()
        self._last_ok_at: float | None = None  # monotonic of last confirmed-live probe
        self._last_recovery_at: float | None = None  # monotonic of last reconnect try
        self._quarantined = False
        self._reason = ""
        _register_gate(self)

    @property
    def port(self) -> int:
        return self._port

    @property
    def quarantined(self) -> bool:
        return self._quarantined

    def status(self) -> dict:
        """A JSON-safe snapshot for the doctor/health report."""
        with self._lock:
            return {
                "quarantined": self._quarantined,
                "reason": self._reason,
                "port": self._port,
                "ttl_s": self._ttl_s,
            }

    def ensure_live(self, reconnect: Callable[[], None]) -> None:
        """Confirm the daemon is live, else raise before the native op runs.

        Fast path: a probe confirmed live within ``ttl_s`` returns immediately (one
        monotonic comparison). Otherwise a fresh probe runs; a dead daemon quarantines
        the store and raises. If already quarantined, one guarded reconnect attempt is
        made (rate-limited to once per TTL) via ``reconnect``; success leaves
        quarantine and returns, failure stays quarantined and re-raises.

        Args:
            reconnect: The connect-or-spawn seam that rebuilds the native binding
                (raises on failure). Invoked at most once per TTL while quarantined.

        Raises:
            ClioCoreRuntimeLostError: When the daemon is not listening and cannot be
                recovered — always *before* the native binding is touched.
        """
        with self._lock:
            if self._quarantined:
                self._attempt_recovery(reconnect)
                return
            now = time.monotonic()
            if self._last_ok_at is not None and now - self._last_ok_at < self._ttl_s:
                return  # cached-live within TTL: negligible per-op overhead
            if self._probe(self._port):
                self._last_ok_at = now
                return
            self._enter_quarantine("clio_core_daemon_not_listening")
            raise ClioCoreRuntimeLostError(
                "clio-core runtime is not listening on "
                f"127.0.0.1:{self._port}; the shared daemon appears to have died. "
                "The store is quarantined to avoid a native access violation "
                "(clio-core#722).",
                reason="clio_core_daemon_not_listening",
                port=self._port,
            )

    def _attempt_recovery(self, reconnect: Callable[[], None]) -> None:
        """One guarded reconnect attempt per TTL; leave quarantine on success."""
        now = time.monotonic()
        if self._last_recovery_at is not None and now - self._last_recovery_at < self._ttl_s:
            # Rate-limit: a hot loop must not spawn-storm the daemon while it is down.
            raise ClioCoreRuntimeLostError(
                f"clio-core runtime still unavailable on 127.0.0.1:{self._port} "
                "(reconnect back-off); store remains quarantined.",
                reason="clio_core_reconnect_backoff",
                port=self._port,
            )
        self._last_recovery_at = now
        try:
            reconnect()
        except Exception as exc:  # noqa: BLE001 - any reconnect failure -> stay quarantined
            logger.warning(
                "clio-core liveness: reconnect attempt failed "
                "(reason=clio_core_reconnect_failed port=%s error=%s: %s); store stays quarantined",
                self._port,
                type(exc).__name__,
                exc,
            )
            raise ClioCoreRuntimeLostError(
                f"clio-core reconnect to 127.0.0.1:{self._port} failed ({exc}); "
                "store remains quarantined.",
                reason="clio_core_reconnect_failed",
                port=self._port,
            ) from exc
        self._leave_quarantine()
        self._last_ok_at = time.monotonic()
        logger.info(
            "clio-core liveness: shared clio-core runtime recovered "
            "(reason=clio_core_runtime_recovered port=%s); store left quarantine",
            self._port,
        )

    def note_rpc_stalled(self, reason: str = "clio_core_rpc_stalled") -> None:
        """Quarantine after a per-RPC stall ladder exhausts (arc/rpc_liveness, #948 S4).

        A zombie daemon (socket alive, RPC hung) defeats the socket probe, so the stall
        wrapper -- not :meth:`ensure_live` -- detected the loss. Enter the SAME
        quarantine so the NEXT op fails fast via :meth:`ensure_live` (one guarded
        reconnect per TTL) instead of paying the full stall ladder again.
        """
        with self._lock:
            self._enter_quarantine(reason)

    def _enter_quarantine(self, reason: str) -> None:
        self._quarantined = True
        self._reason = reason
        self._last_ok_at = None
        logger.warning(
            "clio-core liveness: quarantining clio-core store "
            "(reason=%s port=%s); ops raise ClioCoreRuntimeLostError until the daemon returns",
            reason,
            self._port,
        )

    def _leave_quarantine(self) -> None:
        self._quarantined = False
        self._reason = ""


# --------------------------------------------------------------------------- #
# process-local gate registry (doctor visibility)
# --------------------------------------------------------------------------- #
#
# Quarantine is per-process in-memory state living on the live ClioCoreStore's gate; the
# doctor cannot see it by probing a socket. Gates register here (weakly, so a GC'd
# store drops out) so an IN-PROCESS health report -- e.g. the gact server's own
# status route -- can surface a wedged store. A separate doctor CLI process holds no
# gate and correctly reports nothing.

_active_gates: "weakref.WeakSet[LivenessGate]" = weakref.WeakSet()
_registry_lock = threading.Lock()


def _register_gate(gate: LivenessGate) -> None:
    with _registry_lock:
        _active_gates.add(gate)


def liveness_snapshot() -> list[dict]:
    """Return a status snapshot of every live gate in this process (doctor input)."""
    with _registry_lock:
        gates = list(_active_gates)
    return [gate.status() for gate in gates]
