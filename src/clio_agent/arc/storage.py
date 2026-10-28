"""ARC record storage through its sole durable backend, clio-core.

:class:`ARCStore` maps opaque bytes to ``(kind, name)`` and provides metadata-only
address discovery. It sits beneath ARC's in-memory cache and B-tree index.
:class:`ClioCoreStore` connects to the per-user CTE daemon, starting it when needed
and stopping owned state through ``atexit``. :func:`make_arc_store` raises typed
:class:`~clio_agent.arc.init_degradation.ArcStoreUnavailableError` on failure;
there is no alternate durable store.
"""

import atexit
import contextlib
import logging
import os
import re
import subprocess
import sys
import threading
import weakref
from collections.abc import Iterator, Sequence
from itertools import batched
from pathlib import Path
from typing import Optional

from clio_agent import paths

# Clean-stop + the shutdown latch live in owner module arc/runtime_stop.py (file-size ratchet,
# #775/#774), re-exported below. Also imported as a MODULE (not just names) so
# ``_ensure_runtime_daemon`` reads the latch flag live, not a stale copy frozen at import time.
from clio_agent.arc import clio_core_attach, runtime_stop
from clio_agent.arc import clio_core_daemon_version as daemon_version

# Daemon port-resolution + socket-liveness helpers live in the liveness owner
# module (#892); blob writes ride the bounded rc=13-class retry module (#893).
from clio_agent.arc.batch_put import BatchPutError, PutRecord
from clio_agent.arc.blob_frame import BlobNameDecodeError, read_framed_blob, stored_body
from clio_agent.arc.clio_core_async_ops import (
    TagIds,
    store_delete,
    store_delete_many,
    store_delete_together,
    store_put,
    store_put_many,
)

# CTE config generation + capacity policy (the bounded ram hot-tier cap) live in their own
# owner module (iowarp/clio-agent#774/#890); re-exported here so callers/tests reaching
# ``storage._default_cte_dir`` / ``default_cte_config_path`` keep working.
from clio_agent.arc.clio_core_config import (  # noqa: F401 - re-exported for callers/tests
    _DEFAULT_CTE_CONFIG_TEMPLATE,
    _cte_yaml_path,
    _default_cte_dir,
    _default_cte_file_capacity,
    _default_cte_ram_capacity,
    default_cte_config_path,
    runtime_state_dir,
)
from clio_agent.arc.clio_core_indexer import (
    CLIO_CORE_SEARCH_UNAVAILABLE,
    bind_client_to_indexer,
    indexer_pool,
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
from clio_agent.arc.companion_write import (
    companion_body,
    companion_sizes,
    drop_shrinking,
    remember_written,
)
from clio_agent.arc.pid_identity import pid_alive as _pid_alive
from clio_agent.arc.pid_identity import pidfile_live_pid
from clio_agent.arc.pid_identity import proc_create_time as _proc_create_time
from clio_agent.arc.read_ahead import forget_deleted, forget_written, held_ahead, read_many

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
from clio_agent.arc.store_protocol import ARCStore  # noqa: F401 - the seam callers import
from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)

# The logical record families ARC persists (a namespace/key prefix in clio-core).
# Single source of truth.
ARC_KINDS: tuple[str, ...] = (
    "conversations",
    "invocations",
    "variants",
    "response_feedback",  # immutable user ratings; never added to model context
    "segments",  # live context plane: one record per (session_id, scope)
    "task_index",  # historical duplicate pointers, retained only for ordinary history cleanup
    "task_lookup",  # derived handle/session/tree lookups, read by exact name only
    "task_open",  # derived open-work obligations, the one small listing done at boot
    "artifact_index",  # artifact registry records, read by exact name only (point gets)
    "session_records",  # one record per session + recent-first index buckets (gact)
)

# Suffix for the optional plain-text companion blob (BM25 discovery, Thread D) a
# backend may store next to a record. NOT a record: scan()/get() skip it.
_SEARCH_SUFFIX = ".text"


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
    log_offset = log_path.stat().st_size if log_path.exists() else 0
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
    watch_daemon_process(proc, log_path=log_path, state_dir=state_dir, log_offset=log_offset)
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
#: The pool this process's native client bound to at attach ("" = the core directly).
#: Read from here, not the environment: the binding is fixed when the client attaches.
_client_pool = ""


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
        try:
            alive = _runtime_alive(_resolve_runtime_port(running))
        except ClioCoreRuntimeLostError:
            _deregister_client()
            raise
        if alive:
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

    A standalone host-global daemon serves process clients on the configured RPC
    port. Default durability is a DRAM hot tier spilling to a file cold tier;
    see :func:`default_cte_config_path` and :meth:`_ensure_runtime`.
    """

    _namespace = ""  # the default (bare-tag) namespace; set per instance in __init__
    _kinds: tuple[str, ...] = ARC_KINDS  # the kinds clear() and prewarm cover
    _attach_verified = False  # the post-attach probe ran (once per process attach)
    _instances: "weakref.WeakSet[ClioCoreStore]" = weakref.WeakSet()

    _initialized = False  # process-global init guard (the runtime inits exactly once)
    _attached_runtime_identity: tuple[int, float] | None = None
    _init_lock = threading.Lock()

    def __init__(
        self,
        *,
        config_path: str = "",
        log_level: str = "error",
        init_settle_s: float = 0.5,
        namespace: str = "",
        kinds: Sequence[str] = ARC_KINDS,
    ) -> None:
        config_path = self._ensure_runtime(config_path, log_level, init_settle_s)  # effective
        import clio_cte_core_ext as cte  # noqa: PLC0415

        self._cte = cte
        self._client = cte.get_cte_client()
        type(self)._instances.add(self)
        # Records of one namespace live under ``<namespace>/<kind>`` tags; the default
        # (empty) namespace keeps the bare ``<kind>`` tags every deployment already has.
        self._namespace = namespace
        self._kinds = tuple(kinds)  # what clear() and the tag prewarm cover
        self._tag_ids = TagIds(cte, self.tag)  # Tag(name) blocks with the GIL: once per kind
        self._config_path = config_path
        self._log_level = log_level
        # 905: search needs the indexer composed AND this client bound to a pool above it.
        self._indexer_present = bool(indexer_pool(config_path) and _client_pool)
        # Liveness gate (#892): every op below routes through this before the native
        # binding, so a dead daemon raises ClioCoreRuntimeLostError instead of AV-ing the
        # host process (clio-core#722). See clio_agent.arc.clio_core_liveness.
        # Native CTE tag/read calls hold the GIL. After this client sees daemon
        # loss, rebuilding its binding in-process can freeze unrelated HTTP work.
        # Daemon loss/replacement still requires process restart. A stalled but
        # verified identical daemon may recover through a GIL-free RPC on this
        # existing client; that path must not rebuild its native binding.
        self._gate = LivenessGate(
            config_path=config_path,
            log_level=log_level,
            ttl_s=0.0,
            auto_recover=False,
            attached_identity=type(self)._attached_runtime_identity,
        )
        # Verify each fresh process attach once. Re-probing a later namespace
        # could release the shared client; subsequent loss belongs to the gate.
        with type(self)._init_lock:
            if not type(self)._attach_verified:
                clio_core_attach.verify_post_attach(
                    self,
                    on_failure=lambda: runtime_stop.release_failed_attach(config_path, log_level),
                )
                type(self)._attach_verified = True
        self._tag_ids.prewarm(self._kinds)
        logger.info(
            "ClioCoreStore active: clio-core is the ARC backend (shared daemon runtime). "
            "The DEFAULT config is a DRAM hot tier + file cold tier; durable + "
            "fault-tolerant tiers (replication, erasure coding) are configured in the "
            "CTE config via CLIO_ARC_STORE_CONFIG."
        )

    # Process-owned, idempotent release_runtime_client runs after application
    # drain, with atexit as its backstop. Namespace instances never release it.

    @classmethod
    def drop_native_handles(cls) -> None:
        """Let go of every store's native client and tag ids (a one-shot command's exit).

        The binding reports each native object still alive at interpreter exit as a
        leak (``nanobind: leaked ...``); a process that has released the runtime has no
        further use for them. Any later call on a store fails instead of reaching clio-core.
        """
        for store in list(cls._instances):
            store._tag_ids.clear()
            store._client = None

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
        global _active_config_path, _active_log_level, _client_pool
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

            # 905: bind the client to the indexer before it attaches (a config without
            # one is reported typed: search is then unavailable).
            _client_pool = bind_client_to_indexer(config_path, os.environ)
            # Do NOT redirect fd 2 (no os.dup2 on stderr): under pytest's fd capture that can SILENTLY
            # ABORT the interpreter; CTP_LOG_LEVEL quiets the C++ logging instead. CLIENT ONLY attach,
            # CHECKED: a failed attach/CTE init raises typed and releases this client (#1401).
            from clio_agent.arc.clio_core_liveness import _daemon_identity  # noqa: PLC0415

            original_identity = _daemon_identity(config_path)
            clio_core_attach.attach_and_initialize(
                cte,
                config_path=config_path,
                port=_resolve_runtime_port(config_path),
                settle_s=settle_s,
                on_failure=lambda: runtime_stop.release_failed_attach(config_path, log_level),
            )
            cls._attached_runtime_identity = (
                original_identity if original_identity == _daemon_identity(config_path) else None
            )
            cls._initialized = True

            # Application drain and atexit share the same idempotent release.
            _active_config_path = config_path
            _active_log_level = log_level
            atexit.register(release_runtime_client, config_path, log_level)
            logger.info("clio-core client attached to shared clio-core runtime")
            return config_path

    # ---- liveness gate (#892) ----

    # GIL-free AsyncTagQuery on a sentinel proves an RPC reply without a data write.
    _HEALTH_PROBE_KIND = ARC_KINDS[0]
    _HEALTH_PROBE_NAME = "__clio_liveness_probe__"

    def _live(self) -> None:
        """Reject lost runtimes; probe the verified original client after RPC stalls."""
        self._gate.ensure_live(
            self._reconnect,
            rpc_probe=lambda: store_rpc_health_probe(
                self,
                kind=self._HEALTH_PROBE_KIND,
                name=self._HEALTH_PROBE_NAME,
                reconnect_client=False,
            ),
        )

    def _reconnect(self) -> None:
        """Retry only through the verified original client; never spawn a replacement."""
        from clio_agent.arc.clio_core_liveness import _daemon_identity  # noqa: PLC0415

        identity = type(self)._attached_runtime_identity
        if (
            identity is None
            or _daemon_identity(self._config_path) != identity
            or not _runtime_alive(self._gate.port)
        ):
            reason = "clio_core_client_restart_required"
            self._gate.note_rpc_stalled(reason)
            raise ClioCoreRuntimeLostError(
                "The attached Core runtime changed or disappeared; restart the agent client.",
                reason=reason,
                port=self._gate.port,
            )

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
        # Bound each async RPC individually, with the GIL released while awaiting it.
        # base64-wrap: CTE GetBlob UTF-8-decodes, so store ascii-safe bytes; framed with
        # its length and padded to a size class so a growing record adds few blocks.
        payload = stored_body(data)
        forget_written(self, kind, (name,))
        if kind == "segments":  # #1339: live-lane audit evidence (one row per put)
            stream_audit("store.put", kind=kind, name=name, size=len(payload))
        guarded_store_rpc(self, "put", store_put, self, kind, name, payload)
        # Optional BM25 companion (Thread D) at <name>.text; scan()/get() skip it.
        companion = name + _SEARCH_SUFFIX
        if search_text is not None:  # padded; a shrinking one is dropped first (MD9)
            pair = [(companion, companion_body(search_text))]
            guarded_store_rpc(self, "put", drop_shrinking, self, kind, pair)
            guarded_store_rpc(self, "put", store_put, self, kind, companion, pair[0][1])
            remember_written(self, kind, pair)
        elif may_carry_companion(kind, name):  # #1334: never for the ``_events`` family
            guarded_store_rpc(self, "put", store_delete, self, kind, companion)  # a stale one
            companion_sizes(self).forget(kind, companion)
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
        companions: list[tuple[str, bytes]] = []
        stale: list[str] = []
        forget_written(self, kind, [record.name for record in records])
        for record in records:
            payload = stored_body(record.data)
            if kind == "segments":  # #1339: live-lane audit evidence (one row per put)
                stream_audit("store.put", kind=kind, name=record.name, size=len(payload))
            blobs.append((record.name, payload))
            if record.search_text is not None:
                companions.append(
                    (record.name + _SEARCH_SUFFIX, companion_body(record.search_text))
                )
            elif may_carry_companion(kind, record.name):
                stale.append(record.name + _SEARCH_SUFFIX)
        if companions:  # a shrinking companion is dropped before the batch (MD9)
            guarded_store_rpc(self, "put", drop_shrinking, self, kind, companions)
        lost = guarded_store_rpc(self, "put", store_put_many, self, kind, blobs + companions)
        remember_written(self, kind, [pair for pair in companions if pair[0] not in lost])
        for companion in stale:
            guarded_store_rpc(self, "put", store_delete, self, kind, companion)
            companion_sizes(self).forget(kind, companion)
        if lost:
            written = [name for name, _ in blobs + companions if name not in lost]
            raise BatchPutError(kind, written, dict(lost))

    def tag(self, kind: str) -> str:
        """The CTE tag holding ``kind`` records in this store's namespace (pure; no RPC)."""
        return f"{self._namespace}/{kind}" if self._namespace else kind

    def get(self, kind: str, name: str) -> Optional[bytes]:
        """Read a record: from a pass's read-ahead when it holds it (arc/read_ahead.py)."""
        held, value = held_ahead(self, kind, name)
        return value if held else self._get_one(kind, name)

    def get_many(self, kind: str, names: Sequence[str]) -> dict[str, Optional[bytes]]:
        """Read independent records together, ``None`` for an absent one (read_many)."""
        return read_many(self, kind, names)

    @guard_store_op("get")
    def _get_one(self, kind: str, name: str) -> Optional[bytes]:
        """Read a validated record, allowing one stale native size-metadata observation."""
        tag = self._cte.Tag(self.tag(kind))
        size = tag.GetBlobSize(name)  # 0 for a missing blob (does not raise)
        if size == 0:
            return None
        return read_framed_blob(name, size, lambda length: tag.GetBlob(name, length, 0))

    @guard_store_op("exists")
    def exists(self, kind: str, name: str) -> bool:
        return self._cte.Tag(self.tag(kind)).GetBlobSize(name) > 0

    def scan(self, kind: str, prefix: str = "") -> Iterator[tuple[str, bytes]]:
        for blob_name in self.scan_names(kind, prefix):
            value = self.get(kind, blob_name)
            if value is not None:
                yield blob_name, value

    def scan_names(self, kind: str, prefix: str = "") -> Iterator[str]:
        """Record names under ``prefix``: clio-core matches the names (a prefix asks for
        that prefix only, never the whole kind); no blob body is read."""
        yield from self.query_names(kind, (re.escape(prefix) + ".*") if prefix else "")

    def query_names(self, kind: str, name_re: str = "") -> list[str]:
        """Record names of ``kind`` fully matching ``name_re`` (``""``: all), asked of
        clio-core by pattern (``BlobQuery``); search companions are skipped."""
        self._live()
        tag = self.tag(kind)

        def _list() -> list[str]:
            try:  # the binding decodes every name; one corrupt name fails the listing
                if not name_re:
                    return list(self._cte.Tag(tag).GetContainedBlobs())
                pool = self._cte.PoolQuery.Dynamic()
                rows = self._client.BlobQuery(re.escape(tag), name_re, 0, pool)
                return [blob for _tag, blob in rows]
            except UnicodeDecodeError as exc:
                raise BlobNameDecodeError(tag, exc) from exc

        blobs = call_with_liveness(
            _list,
            op_name="scan",
            port=self._gate.port,
            reconnect=self._reconnect,
            on_exhausted=self._gate.note_rpc_stalled,
        )
        return [name for name in blobs if not name.endswith(_SEARCH_SUFFIX)]

    @guard_store_op("delete")
    def delete(self, kind: str, name: str) -> None:
        # Tag has no per-blob delete; go through the Client + TagId (async: GIL-free). A
        # missing blob answers a non-zero code (no raise), satisfying the no-op contract.
        forget_deleted(self, kind, (name,))
        store_delete_together(self, kind, (name, name + _SEARCH_SUFFIX))  # and its companion
        companion_sizes(self).forget(kind, name + _SEARCH_SUFFIX)

    def clear(self) -> None:
        # Guard each listing/delete RPC; a progressing multi-blob clear is not a stall.
        companion_sizes(self).clear()
        forget_written(self, "", ())  # every kind: nothing read ahead outlives a clear
        for kind in self._kinds:
            blob_names = guarded_store_rpc(
                self, "clear", lambda k: list(self._cte.Tag(self.tag(k)).GetContainedBlobs()), kind
            )
            for batch in batched(blob_names, 64):
                guarded_store_rpc(self, "clear", store_delete_many, self, kind, batch)

    # ---- semantic discovery (Thread D) ----

    def supports_search(self) -> bool:
        """Real BM25 needs the indexer chimod composed and this client bound above it
        (:mod:`clio_agent.arc.clio_core_indexer`, #905)."""
        return self._indexer_present

    def search_degradation_reason(self) -> str:
        """Typed reason real search is unavailable, or ``""`` when available."""
        return "" if self._indexer_present else CLIO_CORE_SEARCH_UNAVAILABLE

    @guard_store_op("search")
    def search(
        self,
        kind: str,
        query_text: str,
        *,
        name_prefix: str = "",
        name_re: str = "",
        k: int = 10,
    ) -> list[tuple[str, float]]:
        """BM25 search over the plain-text companions; returns ``[(record_name, score)]``
        best-first with the ``.text`` suffix stripped. The indexer also returns
        in-scope blobs that match no query term at score 0; those are not hits."""
        if name_prefix and name_re:
            raise ValueError("search takes name_prefix or name_re, not both")
        stem = f"(?:{name_re})" if name_re else f"{re.escape(name_prefix)}.*"
        tag = re.escape(self.tag(kind))
        results = self._client.SemanticSearch(
            tag, stem + re.escape(_SEARCH_SUFFIX), query_text, k, self._cte.PoolQuery.Dynamic()
        )
        out = [(r.blob_name.removesuffix(_SEARCH_SUFFIX), float(r.score)) for r in results]
        return [(name, score) for name, score in out if score > 0]


def make_arc_store(
    *,
    backend: Optional[str] = None,
    data_dir: "str | Path | None" = None,
    config_path: str = "",
    namespace: str | None = None,
    kinds: Sequence[str] = ARC_KINDS,
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
            cfg, backend=backend, data_dir=data_dir, namespace=ns, kinds=kinds
        )
    raise ValueError(f"unknown CLIO_ARC_STORE {choice!r}; the only store is clio-core ('cte')")
