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
  An inconclusive local probe (for example Windows socket allocation failure)
  instead permits a bounded RPC through the verified original daemon/client.
  A real reply establishes liveness without replacing either native object.
* By default, the next op after quarantine makes **one** guarded reconnect attempt
  (rate-limited to once per TTL) through the injected reconnect seam — the existing
  connect-or-spawn path in :mod:`clio_agent.arc.storage`, which spawns + rebinds and
  itself fails loud if a fresh daemon never binds the port (clio-core#725: a stale
  pidfile is overwritten by that spawn, so we do not hand-delete runtime artifacts).
  Success leaves quarantine (a typed INFO audit record); failure stays quarantined
  and re-raises the typed error. ``ClioCoreStore`` disables native rebinding after
  daemon loss: it can freeze unrelated routes. RPC-stall recovery may instead
  probe the original client while the original daemon identity is still verified.
  Dead, replaced or unidentified daemons require an agent restart.

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

import errno
import logging
import os
import socket
import threading
import time
import weakref
from pathlib import Path
from typing import Callable, Optional

from clio_agent.arc.runtime_crash import read_crash_record, summarize_crash
from clio_agent.errors import ClioError

logger = logging.getLogger(__name__)


def _current_crash_record() -> dict | None:
    """The watcher's typed crash record for the ACTIVE runtime state dir, if any.

    Resolved lazily (not cached) because tests and private-daemon runs repoint
    ``CLIO_RUNTIME_STATE_DIR`` per session. Never raises: an unreadable record
    degrades (with a logged warning inside :func:`read_crash_record`) to the
    base liveness message, which is still typed and quarantining.
    """

    from clio_agent.arc.clio_core_config import runtime_state_dir  # noqa: PLC0415

    return read_crash_record(runtime_state_dir())


_DEFAULT_RUNTIME_PORT = 9413

# The quarantine reason set when the per-RPC stall ladder exhausts against a zombie
# (:mod:`clio_agent.arc.rpc_liveness`). Owned here (the quarantine-reason owner) so the
# gate's recovery can be REASON-AWARE without importing rpc_liveness (which imports this
# module); rpc_liveness re-exports it.
RPC_STALLED_REASON = "clio_core_rpc_stalled"
# The ladder exhausted while the daemon process could not be located, so whether it was
# progressing is unknown: not evidence of a stall. Recovers like a stall (a real RPC).
DAEMON_PID_UNRESOLVED_REASON = "clio_core_daemon_pid_unresolved"
SOCKET_PROBE_UNAVAILABLE_REASON = "clio_core_socket_probe_unavailable"
_RPC_PROBE_RECOVERY_REASONS = frozenset(
    {RPC_STALLED_REASON, DAEMON_PID_UNRESOLVED_REASON, SOCKET_PROBE_UNAVAILABLE_REASON}
)

# Recovery back-off TTL for an ``rpc_stalled`` quarantine. A zombie's RPC-level health
# re-probe (:func:`clio_agent.arc.rpc_liveness.probe_rpc_health`) costs up to
# ``_HEALTH_PROBE_MAX_S`` and must not run on every op, so re-probes are spaced this far
# apart — much wider than the socket-probe TTL, whose reconnect is nearly free. Between
# probes an op fails fast (typed, microseconds), delivering the fail-fast promise.
_RPC_STALLED_RECOVERY_TTL_S = 30.0

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

    Single source: when ``config_path`` declares ``networking.port`` that port wins --
    the daemon is spawned with ``CLIO_SERVER_CONF=config_path`` and the native client
    is pointed at the same file (:mod:`clio_agent.arc.clio_core_attach`), so nothing
    else can be what they bind. Otherwise honours the ``CLIO_CORE_PORT`` override, then
    clio-core's own lookup order (``$CLIO_SERVER_CONF`` / ``$CHI_SERVER_CONF``,
    ``~/.clio/clio.yaml``), defaulting to :data:`_DEFAULT_RUNTIME_PORT`.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    declared = _read_yaml_port(config_path) if config_path else None
    if declared is not None:
        return declared
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
        str(Path.home() / ".clio" / "clio.yaml"),
    ]
    for cand in candidates:
        if cand:
            port = _read_yaml_port(cand)
            if port is not None:
                return port
    return _DEFAULT_RUNTIME_PORT


def _runtime_alive(port: int) -> bool:
    """Return listener presence; raise when the local probe cannot establish it.

    Only a refused connection proves absence. Timeouts and local socket exhaustion
    cannot authorize daemon replacement or permanent dead-client quarantine.
    """
    try:
        # Windows can take two seconds to return WSAECONNREFUSED on loopback.
        # A shorter deadline makes a closed port indistinguishable from a timeout.
        with socket.create_connection(("127.0.0.1", port), timeout=3.0):
            return True
    except OSError as error:
        if error.errno == errno.ECONNREFUSED or getattr(error, "winerror", None) == 10061:
            return False
        logger.warning(
            "Core socket probe failed: port=%s error_type=%s errno=%s winerror=%s detail=%s",
            port,
            type(error).__name__,
            error.errno,
            getattr(error, "winerror", None),
            error,
        )
        raise ClioCoreRuntimeLostError(
            f"Cannot probe the Core endpoint on 127.0.0.1:{port}: {error}",
            reason=SOCKET_PROBE_UNAVAILABLE_REASON,
            port=port,
            details={
                "errno": error.errno,
                "winerror": getattr(error, "winerror", None),
                "recovery_actions": ["retry", "run_clio_doctor"],
            },
        ) from error


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


def runtime_socket_status(
    port: int, *, probe: Callable[[int], bool] | None = None
) -> tuple[bool, str | None]:
    """Report an inconclusive socket probe without asserting daemon death."""
    try:
        return (probe or _runtime_alive)(port), None
    except ClioCoreRuntimeLostError as error:
        if error.details.get("reason") != SOCKET_PROBE_UNAVAILABLE_REASON:
            raise
        return False, SOCKET_PROBE_UNAVAILABLE_REASON


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
        self.degradation_reason = reason
        merged: dict = {
            "reason": reason,
            "recovery_actions": [
                "restart_clio_core_daemon",
                "run_clio_doctor",
                "retry",
            ],
        }
        if port is not None:
            merged["port"] = port
        if details:
            merged.update(details)
        # #1148: if the spawner's watcher recorded a daemon crash, NAME it here —
        # every quarantine reason then reads "the daemon CRASHED with 0x…", never
        # a vague "not listening" that gets misread as an environment flake.
        crash = _current_crash_record()
        if crash is not None:
            merged["daemon_crash"] = crash
            message = f"{message} {summarize_crash(crash)}"
        super().__init__(message, error_type="arc_runtime_lost", details=merged)


def _daemon_identity(config_path: str) -> tuple[int, float] | None:
    """Identify the attached daemon without accepting a recycled PID."""
    import psutil  # noqa: PLC0415

    from clio_agent.arc.clio_core_daemon import _resolve_daemon_pid  # noqa: PLC0415

    pid, _ = _resolve_daemon_pid(config_path, os.environ)
    if pid is None:
        return None
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        return pid, process.create_time()
    except psutil.Error:
        return None


class LivenessGate:
    """TTL-cached daemon-liveness gate with quarantine and optional reconnect.

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
        auto_recover: Permit native rebinding when true. When false, only an RPC
            probe of the verified original daemon/client can recover a stall.
        attached_identity: Daemon identity captured by the native client attach.
    """

    def __init__(
        self,
        *,
        config_path: str = "",
        log_level: str = "error",
        probe: Callable[[int], bool] | None = None,
        ttl_s: float | None = None,
        auto_recover: bool = True,
        attached_identity: tuple[int, float] | None = None,
    ) -> None:
        self._config_path = config_path
        self._log_level = log_level
        self._probe = probe or _runtime_alive
        self._port = _resolve_runtime_port(config_path)
        self._ttl_s = _resolve_liveness_ttl_s() if ttl_s is None else float(ttl_s)
        self._auto_recover = auto_recover
        self._attached_identity = attached_identity
        self._lock = threading.RLock()
        self._last_ok_at: float | None = None  # monotonic of last confirmed-live probe
        self._last_recovery_at: float | None = None  # monotonic of last reconnect try
        self._quarantined = False
        self._reason = ""
        self._quarantine_revision = 0
        self._recovery_in_progress = False
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
            snapshot = {
                "quarantined": self._quarantined,
                "reason": self._reason,
                "port": self._port,
                "ttl_s": self._ttl_s,
            }
            identity = self._attached_identity
            requires_restart = self._quarantined and (
                not self._auto_recover or self._reason == SOCKET_PROBE_UNAVAILABLE_REASON
            )
        snapshot["restart_required"] = requires_restart and not (
            snapshot["reason"] in _RPC_PROBE_RECOVERY_REASONS
            and identity is not None
            and _daemon_identity(self._config_path) == identity
        )
        return snapshot

    def ensure_live(
        self,
        reconnect: Callable[[], None],
        rpc_probe: Callable[[], bool] | None = None,
    ) -> None:
        """Confirm the daemon is live, else raise before the native op runs.

        Fast path: a probe confirmed live within ``ttl_s`` returns immediately (one
        monotonic comparison). Otherwise a fresh probe runs; a dead daemon quarantines
        the store and raises. If already quarantined, one guarded recovery attempt is
        made (rate-limited to once per recovery TTL) when ``auto_recover`` is true;
        otherwise the gate requires a process restart to establish a fresh client.

        Recovery is REASON-AWARE. A socket-loss quarantine
        (``clio_core_daemon_not_listening``) recovers through ``reconnect`` — the
        connect-or-spawn seam gated by the socket probe, which is sound because a
        connection-refused daemon is genuinely gone. An ``rpc_stalled`` quarantine
        (:data:`RPC_STALLED_REASON`) is a ZOMBIE whose socket still accepts, so a socket
        probe / plain reconnect passes BY DEFINITION and would dissolve the quarantine
        into another full stall ladder. It instead recovers only through ``rpc_probe``,
        a real RPC-level health check; without one it stays quarantined.
        An inconclusive local socket probe also uses a real RPC, but only through
        the verified original daemon/client; it never permits a replacement spawn.

        Args:
            reconnect: The connect-or-spawn seam that rebuilds the native binding
                (raises on failure). Used for socket-loss recovery.
            rpc_probe: An RPC-LEVEL health probe ``() -> bool`` (returns True iff a real
                RPC answered) used for ``rpc_stalled`` recovery. ``None`` => an
                ``rpc_stalled`` quarantine cannot be left this op (fails fast, typed).

        Raises:
            ClioCoreRuntimeLostError: When the daemon is not listening and cannot be
                recovered — always *before* the native binding is touched.
        """
        with self._lock:
            if self._quarantined:
                recover = True
            else:
                recover = False
                now = time.monotonic()
                if self._last_ok_at is not None and now - self._last_ok_at < self._ttl_s:
                    return
                try:
                    alive = self._probe(self._port)
                except ClioCoreRuntimeLostError as error:
                    if error.details.get("reason") != SOCKET_PROBE_UNAVAILABLE_REASON:
                        raise
                    self._enter_quarantine(SOCKET_PROBE_UNAVAILABLE_REASON)
                    recover = True
                else:
                    if alive:
                        self._last_ok_at = now
                        return
                    self._enter_quarantine("clio_core_daemon_not_listening")
        if recover:
            self._attempt_recovery(reconnect, rpc_probe)
            return
        raise ClioCoreRuntimeLostError(
            "clio-core runtime is not listening on "
            f"127.0.0.1:{self._port}; the shared daemon appears to have died. "
            "The store is quarantined to avoid a native access violation "
            "(clio-core#722).",
            reason="clio_core_daemon_not_listening",
            port=self._port,
            details=(
                {"recovery_actions": ["restart_clio_agent", "run_clio_doctor"]}
                if not self._auto_recover
                else None
            ),
        )

    def _recovery_ttl_s(self) -> float:
        """Rate-limit window for the current quarantine reason.

        An ``rpc_stalled`` re-probe is expensive (a real RPC that may hang up to the
        health-probe window), so it is spaced far wider than a socket-loss reconnect.
        """
        return (
            _RPC_STALLED_RECOVERY_TTL_S
            if self._reason in {RPC_STALLED_REASON, DAEMON_PID_UNRESOLVED_REASON}
            else self._ttl_s
        )

    def _attempt_recovery(
        self,
        reconnect: Callable[[], None],
        rpc_probe: Callable[[], bool] | None,
    ) -> None:
        """One guarded recovery attempt per recovery TTL; leave quarantine on success.

        Reason-aware: an ``rpc_stalled`` quarantine recovers only via ``rpc_probe`` (a
        real RPC), a socket-loss quarantine via ``reconnect`` (the socket-gated
        connect-or-spawn seam).
        """
        with self._lock:
            if not self._quarantined:
                return
            now = time.monotonic()
            if self._recovery_in_progress or (
                self._last_recovery_at is not None
                and now - self._last_recovery_at < self._recovery_ttl_s()
            ):
                raise ClioCoreRuntimeLostError(
                    f"clio-core runtime still unavailable on 127.0.0.1:{self._port} "
                    "(recovery back-off); store remains quarantined.",
                    reason="clio_core_reconnect_backoff",
                    port=self._port,
                )
            if not self._auto_recover and not (
                rpc_probe is not None
                and self._attached_identity is not None
                and self._reason in _RPC_PROBE_RECOVERY_REASONS
            ):
                raise ClioCoreRuntimeLostError(
                    "The original Core client cannot be recovered; restart the agent.",
                    reason="clio_core_client_restart_required",
                    port=self._port,
                    details={"recovery_actions": ["restart_clio_agent", "run_clio_doctor"]},
                )
            revision, reason, identity = (
                self._quarantine_revision,
                self._reason,
                self._attached_identity,
            )
            self._last_recovery_at = now
            self._recovery_in_progress = True
        try:
            original_client_only = (
                not self._auto_recover or reason == SOCKET_PROBE_UNAVAILABLE_REASON
            )
            if reason in _RPC_PROBE_RECOVERY_REASONS:
                if original_client_only and (
                    identity is None or _daemon_identity(self._config_path) != identity
                ):
                    raise ClioCoreRuntimeLostError(
                        "The original Core process changed before its recovery probe.",
                        reason="clio_core_client_restart_required",
                        port=self._port,
                    )
                if rpc_probe is None or not rpc_probe():
                    raise ClioCoreRuntimeLostError(
                        "Core has not answered its recovery probe; store remains quarantined.",
                        reason=reason,
                        port=self._port,
                    )
            else:
                try:
                    reconnect()
                except Exception as error:
                    raise ClioCoreRuntimeLostError(
                        "Core reconnect failed; store remains quarantined.",
                        reason="clio_core_reconnect_failed",
                        port=self._port,
                    ) from error
            current_identity = _daemon_identity(self._config_path) if original_client_only else None
            with self._lock:
                if self._quarantine_revision != revision or (
                    original_client_only and current_identity != identity
                ):
                    raise ClioCoreRuntimeLostError(
                        "Core state changed during its recovery probe; retry after reconciliation.",
                        reason="clio_core_reconnect_backoff",
                        port=self._port,
                    )
                self._leave_quarantine()
                self._last_ok_at = time.monotonic()
            logger.info(
                "clio-core liveness: shared clio-core runtime recovered "
                "(reason=clio_core_runtime_recovered port=%s); store left quarantine",
                self._port,
            )
        finally:
            with self._lock:
                self._recovery_in_progress = False

    def note_rpc_stalled(self, reason: str = RPC_STALLED_REASON) -> None:
        """Quarantine after a per-RPC stall ladder exhausts (arc/rpc_liveness, #948 S4).

        A zombie daemon (socket alive, RPC hung) defeats the socket probe, so the stall
        wrapper -- not :meth:`ensure_live` -- detected the loss. Enter the SAME
        quarantine so the NEXT op fails fast via :meth:`ensure_live`. The recovery clock
        is armed HERE (``_last_recovery_at``) so the immediately-following op is
        rate-limited (fails fast in microseconds) rather than paying an RPC re-probe;
        the first re-probe is spaced one recovery TTL out.
        """
        with self._lock:
            self._enter_quarantine(reason)
            self._last_recovery_at = time.monotonic()

    def _enter_quarantine(self, reason: str) -> None:
        self._quarantine_revision += 1
        self._quarantined = True
        self._reason = reason
        self._last_ok_at = None
        logger.warning(
            "clio-core liveness: quarantining clio-core store "
            "(reason=%s port=%s); ops raise ClioCoreRuntimeLostError until recovery",
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
