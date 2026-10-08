"""Persistent record backends for ARC.

This module defines the storage seam ARC records go through and its one backend:
clio-core. It is the durable tier beneath the in-memory hot layer (``LRUCache`` +
``BTreeIndex`` in ``memory.py``).

The seam -- :class:`ARCStore` (a ``Protocol``): ``put(kind, name, data, search_text=...)``
/ ``get(kind, name)`` / ``scan(kind, prefix)`` over opaque ``bytes`` keyed by
``(kind, name)``.

:class:`ClioCoreStore` -- the clio-core CTE (Convergent Tiered Environment) binding,
connecting to a per-user daemon (connect-or-spawn, stopped at interpreter exit via
``atexit``). clio-core is THE context store: :func:`make_arc_store` raises a typed
:class:`~clio_agent.arc.init_degradation.ArcStoreUnavailableError` when it cannot be
brought up; there is no other store.
"""

import atexit
import contextlib
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from clio_agent import paths

# Clean-stop + the shutdown latch live in owner module arc/runtime_stop.py (file-size ratchet,
# #775/#774), re-exported below. Also imported as a MODULE (not just names) so
# ``_ensure_runtime_daemon`` reads the latch flag live, not a stale copy frozen at import time.
from clio_agent.arc import clio_core_attach, runtime_stop
from clio_agent.arc import clio_core_daemon_version as daemon_version

# Daemon port-resolution + socket-liveness helpers live in the liveness owner
# module (#892); blob writes ride the bounded rc=13-class retry module (#893).
from clio_agent.arc.batch_put import BatchPutError, PutRecord
from clio_agent.arc.blob_frame import BlobNameDecodeError, frame, unframe
from clio_agent.arc.clio_core_async_ops import TagIds, store_delete, store_put, store_put_many

# CTE config generation + capacity policy (the bounded ram hot-tier cap) live in their own
# owner module (iowarp/clio-agent#774/#890); re-exported here so callers/tests reaching
# ``storage._default_cte_dir`` / ``default_cte_config_path`` keep working.
from clio_agent.arc.clio_core_config import (  # noqa: F401 - re-exported for callers/tests
    _DEFAULT_CTE_CONFIG_TEMPLATE,
    CLIO_CORE_SEARCH_INDEXER_ABSENT,
    _cte_yaml_path,
    _default_cte_dir,
    _default_cte_file_capacity,
    _default_cte_ram_capacity,
    default_cte_config_path,
    indexer_chimod_present,
    runtime_state_dir,
    warn_if_search_indexer_absent,
)
from clio_agent.arc.clio_core_liveness import (  # noqa: F401 - re-exported for callers/tests
    _DEFAULT_RUNTIME_PORT,
    ClioCoreRuntimeLostError,
    LivenessGate,
    _read_yaml_port,
    _resolve_runtime_port,
    _runtime_alive,
)
from clio_agent.arc.companion_policy import may_carry_companion
from clio_agent.arc.pid_identity import pid_alive as _pid_alive
from clio_agent.arc.pid_identity import pidfile_live_pid
from clio_agent.arc.pid_identity import proc_create_time as _proc_create_time

# Per-RPC stall guard (#948 S4): every native op below runs through this so a ZOMBIE
# daemon (socket alive, RPC hung) degrades typed instead of freezing the caller.
from clio_agent.arc.rpc_liveness import (
    call_with_liveness,
    guard_store_op,
    guarded_store_rpc,
    store_rpc_health_probe,
)
from clio_agent.arc.runtime_crash import (
    clear_crash_record,
    daemon_start_work,
    wait_for_spawned_daemon,
    watch_daemon_process,
)

# Per-OS spawn primitives live in owner module (#1148); re-exported for callers/tests.
from clio_agent.arc.runtime_spawn import (  # noqa: F401 - re-exported for callers/tests
    _detached_popen_kwargs,
    _dynamic_library_env_var,
    _runtime_launcher_path,
)
from clio_agent.arc.runtime_stop import (  # noqa: F401 - re-exported for callers/tests
    RuntimeShutdownInProgress,
    prepare_runtime_shutdown,
    reset_runtime_shutdown,
)
from clio_agent.arc.runtime_stop import stop_runtime_daemon as _stop_runtime_daemon
from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)

# The logical record families ARC persists (a namespace/key prefix in clio-core).
# Single source of truth.
ARC_KINDS: tuple[str, ...] = (
    "conversations",
    "invocations",
    "variants",
    "segments",  # live context plane: one record per (session_id, scope)
)

# Suffix for the optional plain-text companion blob (BM25 discovery, Thread D) a
# backend may store next to a record. NOT a record: scan()/get() skip it.
_SEARCH_SUFFIX = ".text"


@runtime_checkable
class ARCStore(Protocol):
    """Narrow persistence seam for ARC's record kinds.

    A record is addressed by ``(kind, name)``: ``kind`` is one of :data:`ARC_KINDS`; ``name`` is the
    record stem (no extension). The store owns the physical layout and tiering, so ARC never touches
    the filesystem directly. clio-core maps ``(kind, name)`` onto namespaced, multi-tier
    storage.
    """

    def put(
        self,
        kind: str,
        name: str,
        data: bytes,
        *,
        tier: str = "warm",
        search_text: Optional[str] = None,
    ) -> None:
        """Persist ``data`` for ``(kind, name)`` (overwrites).

        ``search_text`` (optional) is a plain-text projection of the record for BM25 semantic
        discovery (Thread D); a backend may index it. ``None`` drops any existing companion.
        """
        ...

    def put_many(self, kind: str, records: Sequence[PutRecord]) -> None:
        """Put independent records together (no order between them).

        Every record is attempted; a store that lost any raises
        :class:`~clio_agent.arc.batch_put.BatchPutError` naming what was written.
        """
        ...

    def get(self, kind: str, name: str) -> Optional[bytes]:
        """Return bytes for ``(kind, name)`` or ``None`` if absent."""
        ...

    def exists(self, kind: str, name: str) -> bool:
        """Return whether a record exists for ``(kind, name)``."""
        ...

    def scan(self, kind: str, prefix: str = "") -> Iterator[tuple[str, bytes]]:
        """Yield ``(name, data)`` for every record in ``kind`` whose name
        starts with ``prefix`` (``""`` = all). Order is unspecified."""
        ...

    def delete(self, kind: str, name: str) -> None:
        """Delete the record for ``(kind, name)`` if present (no-op if absent)."""
        ...

    def clear(self) -> None:
        """Delete all persisted records across all kinds."""
        ...

    def supports_search(self) -> bool:
        """Whether :meth:`search` does real (e.g. BM25) semantic ranking."""
        ...

    def search(
        self, kind: str, query_text: str, *, name_prefix: str = "", k: int = 10
    ) -> list[tuple[str, float]]:
        """Rank records in ``kind`` (name starting with ``name_prefix``) by relevance
        to ``query_text``. Returns ``[(name, score)]`` best-first. Backends without a
        search index may return a degraded ranking (see ``supports_search``)."""
        ...


@contextlib.contextmanager
def _runtime_spawn_lock() -> "Iterator[None]":
    """Host-global advisory lock serialising the spawn + refcount decisions.

    Without this, two clio-agent processes that both observe "no runtime" would both run ``clio_run
    start`` and the loser would FATAL on the already-bound port; it also serialises a client's
    release (last-one-out stop) against another client attaching. The lock lives at a fixed host
    path (:func:`runtime_state_dir`, NOT per-workspace) so it coordinates every clio-agent on the
    machine sharing that state dir. ``filelock`` is cross-platform (fcntl on POSIX, msvcrt on
    Windows) so the coordination holds on Linux, macOS, and Windows.
    """
    from filelock import FileLock  # noqa: PLC0415

    lock = FileLock(str(runtime_state_dir() / "clio-runtime.lock"))
    with lock:
        yield


def _spawn_runtime_daemon(iowarp_core: object, config_path: str, log_level: str) -> None:
    """Launch the standalone clio-core runtime daemon (``clio_run start``), detached.

    Detached so it outlives the spawning process and becomes the shared instance every client
    attaches to. The OS dynamic-library path env var is set to the iowarp_core lib dir (the
    in-process path relies on an RTLD_GLOBAL preload the standalone binary does not get);
    ``CLIO_SERVER_CONF`` points the daemon at the same config the clients use so it composes the
    matching storage tiers. Cross-platform: launcher name, lib env var, and detach flags are
    resolved per-OS (clio-core deploys identically on all three).
    """
    exe = _runtime_launcher_path(iowarp_core)
    if exe is None:
        raise RuntimeError(
            f"clio-core runtime launcher (clio_run) not found in "
            f"{iowarp_core.get_bin_dir()!r}; cannot spawn the shared clio-core daemon "  # type: ignore[attr-defined]
            "(reinstall the iowarp-core package; run clio doctor)."
        )
    lib_dir = iowarp_core.get_lib_dir()  # type: ignore[attr-defined]
    env = os.environ.copy()
    lib_var = _dynamic_library_env_var()
    env[lib_var] = lib_dir + os.pathsep + env.get(lib_var, "")
    env.setdefault("CTP_LOG_LEVEL", log_level)
    if config_path:
        env["CLIO_SERVER_CONF"] = config_path
    state_dir = runtime_state_dir()
    log_path = state_dir / "clio-runtime.log"
    clear_crash_record(state_dir)  # fresh spawn, fresh slate (#1148)
    log_fh = open(log_path, "ab")  # noqa: SIM115 - handed to the detached child

    def _spawn(*, breakaway: bool) -> "subprocess.Popen[bytes]":
        return subprocess.Popen(  # type: ignore[call-overload]  # noqa: S603 - fixed launcher path
            [exe, "start"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            **_detached_popen_kwargs(breakaway=breakaway),
        )

    try:
        try:
            proc = _spawn(breakaway=True)
        except PermissionError:
            # ERROR_ACCESS_DENIED from CreateProcess: this process sits inside a Job Object that
            # forbids breakaway (a CI runner, a sandbox, a managed desktop). Retrying without the
            # flag is the only way to get a daemon at all, and it costs the #900 property -- the
            # daemon now dies with the enclosing job instead of surviving a hard-kill -- so it is
            # reported, never taken silently.
            if not sys.platform.startswith("win"):
                raise
            logger.warning(
                "clio-core daemon spawn could not break away from the enclosing "
                "Job Object (reason=job_object_breakaway_denied); retrying attached. "
                "The shared daemon will now exit when that job closes."
            )
            proc = _spawn(breakaway=False)
    finally:
        log_fh.close()
    # The daemon must die loudly in OUR channels (#1148): on abnormal exit the
    # watcher writes a typed crash record that the liveness gate folds into its
    # ClioCoreRuntimeLostError, so a crash is never misread as an env flake.
    watch_daemon_process(proc, log_path=log_path, state_dir=state_dir)
    proc_pid = proc.pid
    ctime = _proc_create_time(proc_pid)
    _daemon_pidfile().write_text(
        f"{proc_pid} {ctime if ctime is not None else ''}", encoding="utf-8"
    )
    daemon_version.record_daemon_version(state_dir, daemon_binary=exe, config_path=config_path)
    logger.info(
        "spawned shared clio-core runtime daemon: %s start (pid %s, log: %s)",
        exe,
        proc_pid,
        log_path,
    )


# ---- client refcount: "last one out turns off the lights" -------------------
#
# The shared daemon is released when the LAST client detaches ("I leave the TUI,
# everything gets released" — permanence rides the on-disk storage tier, not a warm
# process). Each process registers its PID under ``~/.clio/hosts/<host>/clio-runtime.clients/`` and,
# on graceful shutdown, deregisters + stops the daemon iff no LIVE client remains. A
# SIGKILLed client's stale PID file is pruned by the liveness check (start-time guards
# against PID reuse) on the next register/release — at most one warm instance, no leak.

_client_registered = False  # process-level: are WE in the registry?
# The shutdown latch (desktop Quit forbids late reacquisition) lives in runtime_stop.
_active_config_path = ""  # stashed so atexit/shutdown can stop the right daemon
_active_log_level = "error"


def _client_registry_dir() -> Path:
    return runtime_state_dir() / "clio-runtime.clients"


def _daemon_pidfile() -> Path:
    return runtime_state_dir() / "clio-runtime.pid"


def _register_client() -> None:
    """Mark this process as an attached client (idempotent within the process)."""
    global _client_registered
    reg = _client_registry_dir()
    reg.mkdir(parents=True, exist_ok=True)
    ctime = _proc_create_time(os.getpid())
    (reg / str(os.getpid())).write_text("" if ctime is None else repr(ctime), encoding="utf-8")
    _client_registered = True


def _deregister_client() -> None:
    global _client_registered
    with contextlib.suppress(OSError):
        (_client_registry_dir() / str(os.getpid())).unlink()
    _client_registered = False


def _live_client_pids() -> "list[int]":
    """Return live client PIDs, pruning stale (dead / PID-reused / garbled) entries."""
    reg = _client_registry_dir()
    if not reg.is_dir():
        return []
    live: list[int] = []
    for entry in reg.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            raw = entry.read_text(encoding="utf-8").strip()
            recorded: Optional[float] = float(raw) if raw else None
        except (OSError, ValueError):
            recorded = None
        if _pid_alive(pid, recorded):
            live.append(pid)
        else:
            with contextlib.suppress(OSError):
                entry.unlink()
    return live


def _kill_daemon_pidfile() -> None:
    """Compatibility wrapper for the runtime-stop owner's PID-file cleanup."""

    runtime_stop.kill_daemon_pidfile()


def release_runtime_client(config_path: str = "", log_level: str = "error") -> None:
    """Deregister this process; stop the shared daemon iff it was the last live client.

    Idempotent and safe to call from multiple shutdown paths (gact lifespan + atexit).
    Serialised by the host-global lock so a concurrent client that is just attaching
    (which registers under the same lock) is never stopped out from under.
    """
    global _client_registered
    if not _client_registered:
        return
    with _runtime_spawn_lock():
        if not _client_registered:
            return
        _deregister_client()
        if not _live_client_pids():
            _stop_runtime_daemon(config_path or _active_config_path, log_level)


def cleanup_runtime_after_client_crash(
    config_path: str = "",
    log_level: str = "error",
    *,
    wait_timeout_seconds: float = 0.0,
) -> bool:
    """Prune crashed clients and stop clio-core only when none remain live."""

    return runtime_stop.cleanup_runtime_after_client_crash(
        config_path, log_level, wait_timeout_seconds=wait_timeout_seconds
    )


def _ensure_runtime_daemon(iowarp_core: object, config_path: str, log_level: str) -> str:
    """Connect-or-spawn + register: ensure the machine's daemon is up and count this client.

    All under the host-global lock so the spawn decision AND the client registration are atomic
    w.r.t. a concurrent client's release (last-one-out stop). Registers THIS process as an attached
    client before returning, so no concurrent release can stop the daemon we are about to connect
    to. FAIL LOUD if a spawned daemon never binds the RPC port. A daemon this process did not spawn
    is version-gated and its config adopted (first config wins; ``clio_core_daemon_version``).
    The latch check is the first statement UNDER the lock: a caller blocked on the lock (behind a
    concurrent ``release_runtime_client``) must re-check once it holds it.

    Returns:
        The EFFECTIVE config: ``config_path`` when this call spawns, else the daemon's.
    """
    port = _resolve_runtime_port(config_path)
    with _runtime_spawn_lock():
        if runtime_stop._runtime_shutdown_requested:
            raise RuntimeShutdownInProgress("clio-core runtime is shutting down")
        _register_client()  # prunes nothing here; release-side prunes. We are now live.
        state = runtime_state_dir()
        running = daemon_version.running_daemon_config(state, config_path)
        if _runtime_alive(_resolve_runtime_port(running)):
            return daemon_version.resolve_effective_config(
                state, config_path, on_failure=_deregister_client
            )
        starting = pidfile_live_pid(_daemon_pidfile())
        with runtime_stop.kill_spawned_daemon_on_failure():
            if starting is None:
                _spawn_runtime_daemon(iowarp_core, config_path, log_level)
                _wait_for_daemon_start(port, state)
                return config_path
            # A daemon still starting (left running at a ceiling) is adopted, not doubled.
            _wait_for_daemon_start(_resolve_runtime_port(running), state)
        return daemon_version.resolve_effective_config(
            state, config_path, on_failure=_deregister_client
        )


def _wait_for_daemon_start(port: int, state: Path) -> None:
    """Wait (progress-based, typed) for the pidfile daemon to bind ``port``."""
    from clio_agent.arc.daemon_progress import no_progress_window_s  # noqa: PLC0415 - cycle

    wait_for_spawned_daemon(
        port,
        alive=_runtime_alive,
        state_dir=state,
        work=daemon_start_work(pidfile_live_pid(_daemon_pidfile()), state / "clio-runtime.log"),
        no_progress_s=no_progress_window_s(),
    )


class ClioCoreStore:
    """ARCStore backed by a **shared** clio-core runtime (connect-or-spawn).

    Maps ``(kind, name)`` -> ``(CTE tag, CTE blob)``. msgpack payloads are base64-wrapped
    because CTE's ``GetBlob`` UTF-8-decodes in the C++ binding and raises on non-UTF-8 bytes.

    RUNTIME MODEL: the chimaera runtime is a host-global singleton — one runtime binds the RPC
    port (default 9413) and serves many clients. This store runs it as a **standalone daemon**
    that outlives any single client: on first use it spawns the daemon iff none is listening,
    then every process attaches as a pure client, so multiple clio-agent processes share ONE
    instance. See ``_ensure_runtime``.

    DURABILITY: the default CTE config is a DRAM hot tier spilling to a file cold tier
    (:func:`default_cte_config_path`).
    """

    _namespace = ""  # the default (bare-tag) namespace; set per instance in __init__
    _attach_verified = False  # the post-attach probe ran (once per process attach)

    _initialized = False  # process-global init guard (the runtime inits exactly once)
    _init_lock = threading.Lock()

    def __init__(
        self,
        *,
        config_path: str = "",
        log_level: str = "error",
        init_settle_s: float = 0.5,
        namespace: str = "",
    ) -> None:
        config_path = self._ensure_runtime(config_path, log_level, init_settle_s)  # effective
        import clio_cte_core_ext as cte  # noqa: PLC0415

        self._cte = cte
        self._client = cte.get_cte_client()
        # Records of one namespace live under ``<namespace>/<kind>`` tags; the default
        # (empty) namespace keeps the bare ``<kind>`` tags every deployment already has.
        self._namespace = namespace
        self._tag_ids = TagIds(cte, self.tag)  # Tag(name) blocks with the GIL: once per kind
        self._config_path = config_path
        self._log_level = log_level
        # 905: cached once, not re-read per call -- see supports_search() below.
        self._indexer_present = indexer_chimod_present(env=os.environ, config_path=config_path)
        # Liveness gate (#892): every op below routes through this before the native
        # binding, so a dead daemon raises ClioCoreRuntimeLostError instead of AV-ing the
        # host process (clio-core#722). See clio_agent.arc.clio_core_liveness.
        # Native CTE tag/read calls hold the GIL. After this client sees daemon
        # loss, rebuilding its binding in-process can freeze unrelated HTTP work.
        # Keep the store quarantined until the agent process is restarted.
        self._gate = LivenessGate(
            config_path=config_path, log_level=log_level, ttl_s=0.0, auto_recover=False
        )
        # The probe proves the process's FRESH attach answers one real RPC; it runs once
        # per attach. A later store must not re-probe: a failing re-probe would release
        # the shared attach for every store. A daemon lost later is the liveness gate's job.
        with type(self)._init_lock:
            if not type(self)._attach_verified:
                clio_core_attach.verify_post_attach(
                    self,
                    on_failure=lambda: runtime_stop.release_failed_attach(config_path, log_level),
                )
                type(self)._attach_verified = True
        self._tag_ids.prewarm(ARC_KINDS)
        logger.info(
            "ClioCoreStore active: clio-core is the ARC backend (shared daemon runtime). "
            "The DEFAULT config is a DRAM hot tier + file cold tier; durable + "
            "fault-tolerant tiers (replication, erasure coding) are configured in the "
            "CTE config via CLIO_ARC_STORE_CONFIG."
        )

    # NOTE: there is deliberately NO instance ``release()`` method. The shared clio-core
    # runtime is released via the module-level, idempotent (last-one-out, deregister-guarded)
    # :func:`release_runtime_client`. A desktop-managed boot calls it deterministically, once,
    # from the gact lifespan right after the turn drain settles
    # (``desktop_lifecycle.release_runtime_after_drain`` in ``gact/app.py``); ``atexit``,
    # registered below in :meth:`_ensure_runtime`, is the general backstop for every OTHER exit
    # path (bare CLI, a crash, a non-desktop server) -- a second call from atexit after the
    # lifespan already ran is a safe no-op.

    @classmethod
    def _ensure_runtime(cls, config_path: str, log_level: str, settle_s: float) -> str:
        """Attach this process to the shared clio-core runtime (connect-or-spawn).

        Connect-or-spawn: if no clio-core daemon is listening on the configured RPC port,
        spawn one (``clio_run start``, serialized across processes by a file lock); then
        attach as a pure client (``chimaera_init(kClient, default_with_runtime=False)``).
        Runs exactly once per process (``_initialized`` guard). Spawning a standalone
        daemon — rather than ``default_with_runtime=True`` — is what lets multiple
        clio-agent processes share ONE clio-core instance. Returns the EFFECTIVE config
        (the running daemon's when this process attached to one: first config wins).
        """
        global _active_config_path, _active_log_level
        with cls._init_lock:
            if cls._initialized:
                return _active_config_path
            os.environ.setdefault("CTP_LOG_LEVEL", log_level)
            # Import order is load-bearing: iowarp_core does the RTLD_GLOBAL .so
            # preload + seeds ~/.clio/clio.yaml; it MUST precede clio_cte_core_ext.
            # isort:skip keeps ruff from reordering these alphabetically.
            import iowarp_core  # noqa: PLC0415  # isort:skip
            import clio_cte_core_ext as cte  # noqa: PLC0415  # isort:skip

            # Ensure the machine's daemon is up BEFORE connecting (a pure client cannot init
            # otherwise), then attach with ITS config (first config wins), exported so daemon,
            # native client and liveness probe read ONE file (clio_core_attach).
            config_path = _ensure_runtime_daemon(iowarp_core, config_path, log_level)
            clio_core_attach.export_client_config(config_path)

            # 905: clio-core >=2.2.0 needs an indexer chimod for BM25 search; not
            # wired in (unsafe, see clio_core_config's docstring) -- warn loudly.
            warn_if_search_indexer_absent(config_path)
            # Do NOT redirect fd 2 (no os.dup2 on stderr): under pytest's fd capture that can SILENTLY
            # ABORT the interpreter; CTP_LOG_LEVEL quiets the C++ logging instead. CLIENT ONLY attach,
            # CHECKED: a failed attach/CTE init raises typed and releases this client (#1401).
            clio_core_attach.attach_and_initialize(
                cte,
                config_path=config_path,
                port=_resolve_runtime_port(config_path),
                settle_s=settle_s,
                on_failure=lambda: runtime_stop.release_failed_attach(config_path, log_level),
            )
            cls._initialized = True

            # Stash the params and register the last-one-out release with atexit, the general
            # backstop for every exit path (plain CLI, a crash, a non-desktop server). A
            # desktop-managed boot ALSO releases earlier and deterministically, from the gact
            # lifespan right after the turn drain settles
            # (desktop_lifecycle.release_runtime_after_drain in gact/app.py); release_runtime_client
            # is deregister-guarded, so whichever of the two paths runs first does the real work and
            # the other is a no-op.
            _active_config_path = config_path
            _active_log_level = log_level
            atexit.register(release_runtime_client, config_path, log_level)
            logger.info("clio-core client attached to shared clio-core runtime")
            return config_path

    # ---- liveness gate (#892) ----

    # Sentinel for the RPC-level health probe: a key that never exists in a record kind.
    # ``GetBlobSize`` on a missing blob returns 0 on a healthy daemon (no write, no raise)
    # and HANGS on a zombie — a cheap, side-effect-free liveness RPC.
    _HEALTH_PROBE_KIND = ARC_KINDS[0]
    _HEALTH_PROBE_NAME = "__clio_liveness_probe__"

    def _live(self) -> None:
        """Gate an op: raise ``ClioCoreRuntimeLostError`` before the native binding if dead.

        Recovery from an ``rpc_stalled`` (zombie) quarantine needs a REAL RPC to answer,
        not just a socket reconnect a zombie passes — hence the ``rpc_probe`` seam.
        """
        self._gate.ensure_live(
            self._reconnect,
            rpc_probe=lambda: store_rpc_health_probe(
                self, kind=self._HEALTH_PROBE_KIND, name=self._HEALTH_PROBE_NAME
            ),
        )

    def _reconnect(self) -> None:
        """Rebuild the clio-core client binding via the connect-or-spawn seam (one attempt).

        Reuses :func:`_ensure_runtime_daemon` (spawns + rebinds under the host-global
        lock, FAILS LOUD if a fresh daemon never binds the port) then re-fetches the
        native client handle. NOTE: a ZOMBIE whose socket still ACCEPTS is seen as
        "alive" there, so this refreshes the handle without respawning — a persistent
        zombie then exhausts the stall ladder into the typed degrade (never a hang).
        """
        import iowarp_core  # noqa: PLC0415

        _ensure_runtime_daemon(iowarp_core, self._config_path, self._log_level)
        self._client = self._cte.get_cte_client()
        self._tag_ids.clear()  # tag ids are daemon state; re-resolve after a reconnect

    # ---- ARCStore Protocol ----

    def put(
        self,
        kind: str,
        name: str,
        data: bytes,
        *,
        tier: str = "warm",
        search_text: Optional[str] = None,
    ) -> None:
        # Multi-RPC: each native call is guarded individually so stall_after_s bounds ONE RPC,
        # on the async API (clio_core_async_ops) so a stalled daemon cannot hold the GIL.
        # base64-wrap: CTE GetBlob UTF-8-decodes, so store ascii-safe bytes; framed with
        # its length because a shorter put can leave the old tail (``blob_frame``).
        payload = frame(data)
        if kind == "segments":  # #1339: live-lane audit evidence (one row per put)
            stream_audit("store.put", kind=kind, name=name, size=len(payload))
        guarded_store_rpc(self, "put", store_put, self, kind, name, payload)
        # Optional BM25 companion (Thread D) at <name>.text; scan()/get() skip it.
        companion = name + _SEARCH_SUFFIX
        if search_text is not None:
            text = search_text.encode("utf-8")
            guarded_store_rpc(self, "put", store_put, self, kind, companion, text)
        elif may_carry_companion(kind, name):  # #1334: never for the ``_events`` family
            guarded_store_rpc(self, "put", store_delete, self, kind, companion)  # a stale one
        # ``tier`` is advisory: the default single DRAM tier makes ReorganizeBlob a no-op.

    def put_many(self, kind: str, records: Sequence[PutRecord]) -> None:
        """Put independent records concurrently on the async path (one stall-guarded
        batch: every ``AsyncPutBlob`` -- bodies and search companions -- is issued, then
        all are awaited). A stale-companion probe runs only for a record that can carry
        one and has no text.

        Raises:
            BatchPutError: Some blob was lost after its bounded retry; the others of the
                batch were written (named in the error).
        """
        blobs: list[tuple[str, bytes]] = []
        stale: list[str] = []
        for record in records:
            payload = frame(record.data)
            if kind == "segments":  # #1339: live-lane audit evidence (one row per put)
                stream_audit("store.put", kind=kind, name=record.name, size=len(payload))
            blobs.append((record.name, payload))
            if record.search_text is not None:
                blobs.append((record.name + _SEARCH_SUFFIX, record.search_text.encode("utf-8")))
            elif may_carry_companion(kind, record.name):
                stale.append(record.name + _SEARCH_SUFFIX)
        lost = guarded_store_rpc(self, "put", store_put_many, self, kind, blobs)
        for companion in stale:
            guarded_store_rpc(self, "put", store_delete, self, kind, companion)
        if lost:
            written = [name for name, _ in blobs if name not in lost]
            raise BatchPutError(kind, written, dict(lost))

    def tag(self, kind: str) -> str:
        """The CTE tag holding ``kind`` records in this store's namespace (pure; no RPC)."""
        return f"{self._namespace}/{kind}" if self._namespace else kind

    @guard_store_op("get")
    def get(self, kind: str, name: str) -> Optional[bytes]:
        tag = self._cte.Tag(self.tag(kind))
        size = tag.GetBlobSize(name)  # 0 for a missing blob (does not raise)
        if size == 0:
            return None
        return unframe(name, tag.GetBlob(name, size, 0))

    @guard_store_op("exists")
    def exists(self, kind: str, name: str) -> bool:
        return self._cte.Tag(self.tag(kind)).GetBlobSize(name) > 0

    def scan(self, kind: str, prefix: str = "") -> Iterator[tuple[str, bytes]]:
        # scan() is a generator: the decorator would guard only building it, not
        # iterating. Guard the ONE listing RPC inline; per-blob reads use guarded get().
        self._live()

        def _list() -> list[str]:
            try:  # the binding decodes every name; one corrupt name fails the listing
                return list(self._cte.Tag(self.tag(kind)).GetContainedBlobs())
            except UnicodeDecodeError as exc:
                raise BlobNameDecodeError(self.tag(kind), exc) from exc

        blobs = call_with_liveness(
            _list,
            op_name="scan",
            port=self._gate.port,
            reconnect=self._reconnect,
            on_exhausted=self._gate.note_rpc_stalled,
        )
        for blob_name in blobs:
            if blob_name.endswith(_SEARCH_SUFFIX):
                continue  # search companion, not a record
            if blob_name.startswith(prefix):
                value = self.get(kind, blob_name)
                if value is not None:
                    yield blob_name, value

    @guard_store_op("delete")
    def delete(self, kind: str, name: str) -> None:
        # Tag has no per-blob delete; go through the Client + TagId (async: GIL-free). A
        # missing blob answers a non-zero code (no raise), satisfying the no-op contract.
        store_delete(self, kind, name)
        store_delete(self, kind, name + _SEARCH_SUFFIX)  # companion (no-op if absent)

    def clear(self) -> None:
        # Multi-RPC: each DelBlob (and the per-kind listing) is guarded INDIVIDUALLY so a
        # long, PROGRESSING clear over many blobs (total > stall_after_s) is never
        # misclassified as a stalled peer; only a single hanging RPC trips the ladder.
        for kind in ARC_KINDS:
            blob_names = guarded_store_rpc(
                self, "clear", lambda k: list(self._cte.Tag(self.tag(k)).GetContainedBlobs()), kind
            )
            for blob_name in blob_names:
                guarded_store_rpc(self, "clear", store_delete, self, kind, blob_name)

    # ---- semantic discovery (Thread D) ----

    def supports_search(self) -> bool:
        """Real BM25 needs the indexer chimod composed (clio_core_config's INDEXER
        CHIMOD note, #905); report the truth instead of a blanket True."""
        return self._indexer_present

    def search_degradation_reason(self) -> str:
        """Typed reason real search is unavailable, or ``""`` when available."""
        return "" if self._indexer_present else CLIO_CORE_SEARCH_INDEXER_ABSENT

    @guard_store_op("search")
    def search(
        self, kind: str, query_text: str, *, name_prefix: str = "", k: int = 10
    ) -> list[tuple[str, float]]:
        """BM25 semantic search over the plain-text companions; returns
        ``[(record_name, score)]`` best-first with the ``.text`` suffix stripped."""
        import re  # noqa: PLC0415

        blob_re = f"{re.escape(name_prefix)}.*{re.escape(_SEARCH_SUFFIX)}"
        results = self._client.SemanticSearch(
            self.tag(kind), blob_re, query_text, k, self._cte.PoolQuery.Dynamic()
        )
        out: list[tuple[str, float]] = []
        for r in results:
            bn = r.blob_name
            if bn.endswith(_SEARCH_SUFFIX):
                bn = bn[: -len(_SEARCH_SUFFIX)]
            out.append((bn, float(r.score)))
        return out


def make_arc_store(
    *,
    backend: Optional[str] = None,
    data_dir: "str | Path | None" = None,
    config_path: str = "",
    namespace: str | None = None,
) -> "ARCStore":
    """Build the ARC persistence backend.

    clio-core is clio-agent's data operator; its CTE (Convergent Tiered Environment)
    is the tiering component that backs the canonical ARC store. Selection (first
    match wins): explicit ``backend`` arg, env ``CLIO_ARC_STORE``, default ``"cte"``.

    clio-core is THE context store: an init failure raises
    :class:`~clio_agent.arc.init_degradation.ArcStoreUnavailableError` (typed reason),
    never another store (supersedes the #897 LocalFS degrade).
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    data_dir = data_dir if data_dir is not None else paths.arc_data_dir()
    resolved = backend or conf.resolve(
        "arc.store", env="CLIO_ARC_STORE", default="cte", cast=conf.as_str
    )
    choice = resolved.strip().lower()
    if choice == "cte":
        cfg = config_path or conf.resolve(
            "arc.store_config", env="CLIO_ARC_STORE_CONFIG", default="", cast=conf.as_str
        )
        if not cfg:
            # Per-workspace ``.clio/core`` config if present, else the seeded default.
            ws_cfg = paths.workspace_core_dir() / "cte.yaml"
            cfg = str(ws_cfg) if ws_cfg.is_file() else default_cte_config_path()
        ns = (
            namespace
            if namespace is not None
            else conf.resolve(
                "arc.namespace", env="CLIO_ARC_NAMESPACE", default="", cast=conf.as_str
            )
        )
        return clio_core_attach.build_tracked_store(
            cfg, backend=backend, data_dir=data_dir, namespace=ns
        )
    raise ValueError(f"unknown CLIO_ARC_STORE {choice!r}; the only store is clio-core ('cte')")
