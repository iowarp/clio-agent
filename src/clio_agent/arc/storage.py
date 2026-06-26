"""IOWarp CTE (Convergent Tiered Environment) storage backend for ARC.

Integrates ARC Memory with IOWarp's multi-tier storage system for automatic
data migration across tiers based on access patterns.

Architecture:
    - Hot tier: In-memory cache (handled by LRUCache in memory.py)
    - Warm tier: SSD/local disk (default for active data)
    - Cold tier: Network storage/HDF5 (for historical data)
    - Archive tier: Tape/long-term storage (for old data)

Tier Migration Policy:
    - Hot → Warm: 1 day (handled by LRU cache eviction)
    - Warm → Cold: 7 days (infrequent access)
    - Cold → Archive: 30 days (historical data)

Graceful Degradation:
    If IOWarp is unavailable, falls back to local filesystem storage.

See PLAN.md v0.3.0 Task 2 for requirements.
"""

import atexit
import base64
import contextlib
import logging
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Dict, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# The logical record families ARC persists. Each maps to one physical
# container in a store (a directory for LocalFSStore; a namespace/key prefix
# for a CTE-backed store). Keep this list as the single source of truth.
ARC_KINDS: tuple[str, ...] = (
    "conversations",
    "invocations",
    "metrics",
    "context",
    "profiles",
    "procedural",
    "variants",
    "segments",  # live context plane: one record per (session_id, scope)
)

# Suffix for the optional plain-text companion blob a backend may store next to a
# record for BM25 semantic discovery (Thread D). Companions are NOT records:
# scan()/get() skip them. Record names must not end with this suffix.
_SEARCH_SUFFIX = ".text"


@runtime_checkable
class ARCStore(Protocol):
    """Narrow persistence seam for ARC's record kinds.

    A record is addressed by ``(kind, name)``: ``kind`` is one of
    :data:`ARC_KINDS`; ``name`` is the record stem (no extension). The store
    owns the physical layout and tiering, so ARC never touches the filesystem
    directly. :class:`LocalFSStore` writes ``<data_dir>/<kind>/<name>.msgpack``;
    a clio-core CTE backend maps the same ``(kind, name)`` onto namespaced,
    multi-tier storage. This Protocol is the seam where that backend plugs in.
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

        ``search_text`` (optional) is a plain-text projection of the record for BM25
        semantic discovery (Thread D); a backend may index it. ``None`` drops any
        existing companion.
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


class LocalFSStore:
    """Default :class:`ARCStore` backed by the local filesystem.

    Lays records out as ``<data_dir>/<kind>/<name>.msgpack`` -- the historical
    on-disk format ARC has always used, so existing data directories are read
    unchanged. This is the extraction of the filesystem code that previously
    lived inline in ``ARCMemory``; the LSM tree remains a separate subsystem.
    """

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._dirs: Dict[str, Path] = {kind: self.data_dir / kind for kind in ARC_KINDS}
        for directory in self._dirs.values():
            directory.mkdir(exist_ok=True)

    def _dir(self, kind: str) -> Path:
        try:
            return self._dirs[kind]
        except KeyError:
            raise ValueError(f"unknown ARC kind {kind!r}; expected one of {ARC_KINDS}") from None

    def put(
        self,
        kind: str,
        name: str,
        data: bytes,
        *,
        tier: str = "warm",
        search_text: Optional[str] = None,
    ) -> None:
        directory = self._dir(kind)
        (directory / f"{name}.msgpack").write_bytes(data)
        # Plain-text companion sidecar for search (Thread D). ``.search`` so the
        # ``*.msgpack`` scan never picks it up as a record.
        companion = directory / f"{name}.search"
        if search_text is not None:
            companion.write_text(search_text, encoding="utf-8")
        elif companion.exists():
            companion.unlink()

    def get(self, kind: str, name: str) -> Optional[bytes]:
        path = self._dir(kind) / f"{name}.msgpack"
        if not path.exists():
            return None
        return path.read_bytes()

    def exists(self, kind: str, name: str) -> bool:
        return (self._dir(kind) / f"{name}.msgpack").exists()

    def scan(self, kind: str, prefix: str = "") -> Iterator[tuple[str, bytes]]:
        for path in self._dir(kind).glob(f"{prefix}*.msgpack"):
            try:
                data = path.read_bytes()
            except OSError:
                continue
            yield path.stem, data

    def delete(self, kind: str, name: str) -> None:
        directory = self._dir(kind)
        for suffix in (".msgpack", ".search"):
            path = directory / f"{name}{suffix}"
            if path.exists():
                path.unlink()

    def clear(self) -> None:
        for directory in self._dirs.values():
            for pattern in ("*.msgpack", "*.search"):
                for path in directory.glob(pattern):
                    path.unlink()

    def supports_search(self) -> bool:
        return False  # naive word-overlap, not BM25 (use CTEStore for real ranking)

    def search(
        self, kind: str, query_text: str, *, name_prefix: str = "", k: int = 10
    ) -> list[tuple[str, float]]:
        """Degraded fallback: rank by query-word overlap over the ``.search``
        companions. Good enough for tests / non-CTE deployments; CTEStore does BM25."""
        terms = {t for t in query_text.lower().split() if t}
        if not terms:
            return []
        scored: list[tuple[str, float]] = []
        for path in self._dir(kind).glob(f"{name_prefix}*.search"):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                continue
            score = sum(1 for w in text.split() if w in terms)
            if score > 0:
                scored.append((path.stem, float(score)))  # .stem drops ".search"
        scored.sort(key=lambda x: -x[1])
        return scored[:k]


# --------------------------------------------------------------------------- #
# clio-core shared-runtime lifecycle (connect-or-spawn)
#
# clio-core's chimaera runtime is a host-global singleton: ONE runtime binds the
# RPC port (default 9413) and serves many clients. ``chimaera_init(kClient, True)``
# would self-start an *embedded* runtime that dies with the calling process and
# holds the port exclusively, so a second clio-agent process FATALs ("already
# running on <port>"). Instead clio-core runs as a SHARED standalone daemon: spawn
# it once (``clio_run start``) iff none is up, then every client attaches with
# ``chimaera_init(kClient, False)``. This realises the user's directive: "multiple
# clients connect to the same instance of clio-core — if no clio-core: spawn();
# connect()."
# --------------------------------------------------------------------------- #

_DEFAULT_RUNTIME_PORT = 9413
_RUNTIME_START_TIMEOUT_S = 30.0


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
    override = os.environ.get("CLIO_CORE_PORT", "").strip()
    if override:
        try:
            return int(override)
        except ValueError:
            logger.warning("ignoring non-integer CLIO_CORE_PORT=%r", override)
    candidates = [
        os.environ.get("CLIO_SERVER_CONF", "").strip(),
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


@contextlib.contextmanager
def _runtime_spawn_lock() -> "Iterator[None]":
    """Host-global advisory lock serialising the spawn decision across processes.

    Without this, two clio-agent processes that both observe "no runtime" would both
    run ``clio_run start`` and the loser would FATAL on the already-bound port. The
    lock lives at a fixed host path (NOT per-workspace) so it coordinates every
    clio-agent on the machine.
    """
    import fcntl  # noqa: PLC0415 - POSIX-only; the CTE backend is Linux-only

    lock_dir = Path.home() / ".clio"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "clio-runtime.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _spawn_runtime_daemon(iowarp_core: object, config_path: str, log_level: str) -> None:
    """Launch the standalone clio-core runtime daemon (``clio_run start``), detached.

    Detached (``start_new_session=True``) so it outlives the spawning process and
    becomes the shared instance every client attaches to. ``LD_LIBRARY_PATH`` is set
    to the iowarp_core lib dir (the in-process path relies on RTLD_GLOBAL preload,
    which the standalone binary does not get); ``CLIO_SERVER_CONF`` points the daemon
    at the same config the clients use so it composes the matching storage tiers.
    """
    bin_dir = iowarp_core.get_bin_dir()  # type: ignore[attr-defined]
    lib_dir = iowarp_core.get_lib_dir()  # type: ignore[attr-defined]
    exe = os.path.join(bin_dir, "clio_run")
    if not os.path.exists(exe):
        raise RuntimeError(
            f"clio-core runtime launcher not found at {exe!r}; cannot spawn the shared "
            "clio-core daemon (set CLIO_ARC_STORE=local to use the LocalFS backend)."
        )
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = lib_dir + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    env.setdefault("CTP_LOG_LEVEL", log_level)
    if config_path:
        env["CLIO_SERVER_CONF"] = config_path
    log_path = Path.home() / ".clio" / "clio-runtime.log"
    log_fh = open(log_path, "ab")  # noqa: SIM115 - handed to the detached child
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed launcher path, not user input
            [exe, "start"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_fh.close()
    proc_pid = proc.pid
    starttime = _proc_starttime(proc_pid)
    _daemon_pidfile().write_text(
        f"{proc_pid} {starttime if starttime is not None else ''}", encoding="utf-8"
    )
    logger.info(
        "spawned shared clio-core runtime daemon: %s start (pid %s, log: %s)",
        exe,
        proc_pid,
        log_path,
    )


# ---- client refcount: "last one out turns off the lights" -------------------
#
# The shared daemon must be released when the LAST client detaches (Jaime: "I leave
# the TUI, everything gets released" — permanence rides the storage tier on disk,
# NOT a warm process). Each clio-agent process registers its PID under
# ``~/.clio/clio-runtime.clients/``; on graceful shutdown it deregisters and, if no
# LIVE client remains, stops the daemon. Crash-safety: a SIGKILLed client cannot
# deregister, so its stale PID file is pruned by liveness check (``/proc`` start-time
# guards against PID reuse) on the next register/release — the daemon is at most one
# reusable warm instance, never a growing leak.

_client_registered = False  # process-level: are WE in the registry?
_active_config_path = ""  # stashed so atexit/shutdown can stop the right daemon
_active_log_level = "error"


def _client_registry_dir() -> Path:
    return Path.home() / ".clio" / "clio-runtime.clients"


def _daemon_pidfile() -> Path:
    return Path.home() / ".clio" / "clio-runtime.pid"


def _proc_starttime(pid: int) -> Optional[int]:
    """Process start-time (clock ticks since boot) from ``/proc/<pid>/stat``, or None.

    Used to defeat PID reuse: a recycled PID gets a different start-time, so a stale
    registry entry won't be mistaken for a live client.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    # comm (field 2) is parenthesised and may contain spaces/parens; split AFTER the
    # last ')'. Then field 22 (starttime) is index 19 of the remainder (fields 3..).
    rparen = data.rfind(b")")
    if rparen == -1:
        return None
    rest = data[rparen + 2 :].split()
    try:
        return int(rest[19])
    except (IndexError, ValueError):
        return None


def _pid_alive(pid: int, recorded_starttime: Optional[int]) -> bool:
    """True if ``pid`` is alive AND (when known) its start-time matches the record."""
    current = _proc_starttime(pid)
    if current is None:
        return False  # no such process
    if recorded_starttime is None:
        return True  # start-time wasn't captured; fall back to bare existence
    return current == recorded_starttime


def _register_client() -> None:
    """Mark this process as an attached client (idempotent within the process)."""
    global _client_registered
    reg = _client_registry_dir()
    reg.mkdir(parents=True, exist_ok=True)
    starttime = _proc_starttime(os.getpid())
    (reg / str(os.getpid())).write_text(
        "" if starttime is None else str(starttime), encoding="utf-8"
    )
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
            recorded: Optional[int] = int(raw) if raw else None
        except (OSError, ValueError):
            recorded = None
        if _pid_alive(pid, recorded):
            live.append(pid)
        else:
            with contextlib.suppress(OSError):
                entry.unlink()
    return live


def _kill_daemon_pidfile() -> None:
    """Fallback teardown: SIGTERM->SIGKILL the daemon recorded in the pidfile.

    Best-effort and PID-reuse-guarded; used only if the clean ``clio_run stop`` path
    fails or we (a non-spawner) have no IPC route. Absent pidfile == nothing to do.
    """
    pidfile = _daemon_pidfile()
    try:
        parts = pidfile.read_text(encoding="utf-8").split()
    except OSError:
        return
    if not parts:
        return
    try:
        pid = int(parts[0])
    except ValueError:
        return
    recorded = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    if not _pid_alive(pid, recorded):
        with contextlib.suppress(OSError):
            pidfile.unlink()
        return
    import signal  # noqa: PLC0415

    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _pid_alive(pid, recorded):
            break
        time.sleep(0.1)
    if _pid_alive(pid, recorded):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        pidfile.unlink()


def _stop_runtime_daemon(config_path: str, log_level: str) -> None:
    """Stop the shared daemon cleanly (``clio_run stop``), with a kill fallback."""
    stopped = False
    try:
        import iowarp_core  # noqa: PLC0415

        exe = os.path.join(iowarp_core.get_bin_dir(), "clio_run")  # type: ignore[attr-defined]
        if os.path.exists(exe):
            env = os.environ.copy()
            env["LD_LIBRARY_PATH"] = (
                iowarp_core.get_lib_dir() + os.pathsep + env.get("LD_LIBRARY_PATH", "")  # type: ignore[attr-defined]
            )
            env.setdefault("CTP_LOG_LEVEL", log_level)
            if config_path:
                env["CLIO_SERVER_CONF"] = config_path
            subprocess.run(  # noqa: S603 - fixed launcher path
                [exe, "stop"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            )
            stopped = True
    except (subprocess.TimeoutExpired, OSError, ImportError):
        stopped = False
    # Confirm the port actually freed; fall back to a direct kill if not.
    if not stopped or _runtime_alive(_resolve_runtime_port(config_path)):
        _kill_daemon_pidfile()
    with contextlib.suppress(OSError):
        _daemon_pidfile().unlink()
    logger.info("released last clio-core client -> stopped shared runtime daemon")


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


def _ensure_runtime_daemon(iowarp_core: object, config_path: str, log_level: str) -> None:
    """Connect-or-spawn + register: ensure a shared daemon is up and count this client.

    All under the host-global lock so the spawn decision AND the client registration
    are atomic w.r.t. a concurrent client's release (last-one-out stop). Registers
    THIS process as an attached client before returning, so no concurrent release can
    stop the daemon we are about to connect to. FAIL LOUD if a spawned daemon never
    binds the RPC port.
    """
    port = _resolve_runtime_port(config_path)
    with _runtime_spawn_lock():
        _register_client()  # prunes nothing here; release-side prunes. We are now live.
        if _runtime_alive(port):
            return
        _spawn_runtime_daemon(iowarp_core, config_path, log_level)
        deadline = time.monotonic() + _RUNTIME_START_TIMEOUT_S
        while time.monotonic() < deadline:
            if _runtime_alive(port):
                return
            time.sleep(0.25)
        raise RuntimeError(
            f"spawned the clio-core runtime daemon but it never bound port {port} within "
            f"{_RUNTIME_START_TIMEOUT_S:.0f}s; see ~/.clio/clio-runtime.log."
        )


class CTEStore:
    """ARCStore backed by a **shared** clio-core CTE runtime (connect-or-spawn).

    Maps ``(kind, name)`` -> ``(CTE tag, CTE blob)``. msgpack payloads are
    base64-wrapped because CTE's ``GetBlob`` UTF-8-decodes in the C++ binding and
    raises on non-UTF-8 bytes.

    RUNTIME MODEL: clio-core's chimaera runtime is a host-global singleton — one
    runtime binds the RPC port (default 9413) and serves many clients. This store
    runs it as a **standalone daemon** that outlives any single client: on first
    use it spawns the daemon (``clio_run start``) iff none is listening, then every
    process attaches as a pure client (``chimaera_init(kClient, default_with_runtime
    =False)``). Multiple clio-agent processes (e.g. two gact servers) therefore
    share ONE clio-core instance instead of each trying to self-start an embedded
    runtime and FATAL-ing on the already-bound port. See ``_ensure_runtime``.

    DURABILITY: the default CTE config is a DRAM hot tier spilling to a file cold
    tier (:func:`default_cte_config_path`). Cross-restart blob-data recovery is WIP
    upstream; for guaranteed disk durability today, select the LocalFS backend
    (``CLIO_ARC_STORE=local``).
    """

    _initialized = False  # process-global init guard (the runtime inits exactly once)
    _init_lock = threading.Lock()

    def __init__(
        self,
        *,
        config_path: str = "",
        log_level: str = "error",
        init_settle_s: float = 0.5,
    ) -> None:
        self._ensure_runtime(config_path, log_level, init_settle_s)
        import clio_cte_core_ext as cte  # noqa: PLC0415

        self._cte = cte
        self._client = cte.get_cte_client()
        self._config_path = config_path
        self._log_level = log_level
        logger.info(
            "CTEStore active: clio-core CTE is the ARC backend (shared daemon runtime). "
            "The DEFAULT config is a DRAM hot tier + file cold tier; durable + "
            "fault-tolerant tiers (replication, erasure coding) are configured in the "
            "CTE config via CLIO_ARC_STORE_CONFIG. Use CLIO_ARC_STORE=local for disk "
            "durability today."
        )

    def release(self) -> None:
        """Detach this process from the shared runtime; stop it if we were the last.

        Wired into the gact server's lifespan shutdown so leaving the TUI releases the
        whole runtime. Idempotent; a no-op if another client is still attached.
        """
        release_runtime_client(self._config_path, self._log_level)

    @classmethod
    def _ensure_runtime(cls, config_path: str, log_level: str, settle_s: float) -> None:
        """Attach this process to the shared clio-core runtime (connect-or-spawn).

        Connect-or-spawn: if no clio-core daemon is listening on the configured RPC
        port, spawn one (``clio_run start``, serialized across processes by a file
        lock); then attach as a pure client (``chimaera_init(kClient, default_with
        _runtime=False)``). Runs exactly once per process (``_initialized`` guard).
        Spawning a standalone daemon — rather than ``default_with_runtime=True`` —
        is what lets multiple clio-agent processes share ONE clio-core instance.
        """
        with cls._init_lock:
            if cls._initialized:
                return
            os.environ.setdefault("CTP_LOG_LEVEL", log_level)
            # Import order is load-bearing: iowarp_core does the RTLD_GLOBAL .so
            # preload + seeds ~/.clio/clio.yaml; it MUST precede clio_cte_core_ext.
            # isort:skip keeps ruff from reordering these alphabetically.
            import iowarp_core  # noqa: PLC0415  # isort:skip
            import clio_cte_core_ext as cte  # noqa: PLC0415  # isort:skip

            # Ensure a shared runtime daemon exists BEFORE connecting (a pure client
            # cannot init against a runtime that is not up).
            _ensure_runtime_daemon(iowarp_core, config_path, log_level)

            # Do NOT redirect fd 2 (no os.dup2 on stderr) here. Under pytest's
            # fd-level capture that clobbers the captured fd and can SILENTLY ABORT
            # the interpreter (exit 1, zero output) depending on capture mode +
            # ambient CTE shared-memory state. CTP_LOG_LEVEL quiets the C++ logging;
            # a one-time startup banner on stderr is an acceptable trade for never
            # crashing the host process.
            #
            # default_with_runtime=False => CLIENT ONLY: attach to the shared daemon,
            # never self-start an embedded (process-bound, port-exclusive) runtime.
            cte.chimaera_init(cte.ChimaeraMode.kClient, False)
            time.sleep(settle_s)  # let the client handshake settle
            cte.initialize_cte(config_path, cte.PoolQuery.Dynamic())  # "" => ~/.clio/clio.yaml
            cls._initialized = True

            # Stash for the release path, and register an atexit fallback so a clean
            # Python exit (legacy agent, tests, scripts) still does last-one-out. The
            # gact server calls release_runtime_client() from its lifespan shutdown for
            # the SIGTERM / TUI-leave path (where atexit does not run).
            global _active_config_path, _active_log_level
            _active_config_path = config_path
            _active_log_level = log_level
            atexit.register(release_runtime_client, config_path, log_level)
            logger.info("CTE client attached to shared clio-core runtime")

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
        # base64-wrap: CTE GetBlob UTF-8-decodes, so store ascii-safe bytes.
        tag = self._cte.Tag(kind)
        tag.PutBlob(name, base64.b64encode(data), 0)
        # Optional plain-text companion for BM25 semantic discovery (Thread D). CTE
        # SemanticSearch tokenises blob payloads, which the base64 record defeats —
        # so a UTF-8 companion at <name>.text carries the searchable text. scan()/get()
        # skip it so it is never mistaken for a record.
        companion = name + _SEARCH_SUFFIX
        if search_text is not None:
            tag.PutBlob(companion, search_text.encode("utf-8"), 0)
        elif tag.GetBlobSize(companion) > 0:
            self._client.DelBlob(tag.GetTagId(), companion)  # drop a now-stale companion
        # ``tier`` is advisory: the default single DRAM tier makes ReorganizeBlob a
        # no-op. Wire tier->score only when a real file/HDD bdev is configured.

    def get(self, kind: str, name: str) -> Optional[bytes]:
        tag = self._cte.Tag(kind)
        size = tag.GetBlobSize(name)  # 0 for a missing blob (does not raise)
        if size == 0:
            return None
        return base64.b64decode(tag.GetBlob(name, size, 0))

    def exists(self, kind: str, name: str) -> bool:
        return self._cte.Tag(kind).GetBlobSize(name) > 0

    def scan(self, kind: str, prefix: str = "") -> Iterator[tuple[str, bytes]]:
        tag = self._cte.Tag(kind)
        for blob_name in tag.GetContainedBlobs():
            if blob_name.endswith(_SEARCH_SUFFIX):
                continue  # search companion, not a record
            if blob_name.startswith(prefix):
                value = self.get(kind, blob_name)
                if value is not None:
                    yield blob_name, value

    def delete(self, kind: str, name: str) -> None:
        # Tag has no per-blob delete; go through the Client + TagId. DelBlob on a
        # missing blob returns False (no raise), satisfying the no-op contract.
        tag = self._cte.Tag(kind)
        tag_id = tag.GetTagId()
        self._client.DelBlob(tag_id, name)
        self._client.DelBlob(tag_id, name + _SEARCH_SUFFIX)  # companion (no-op if absent)

    def clear(self) -> None:
        for kind in ARC_KINDS:
            tag = self._cte.Tag(kind)
            tag_id = tag.GetTagId()
            for blob_name in tag.GetContainedBlobs():
                self._client.DelBlob(tag_id, blob_name)

    # ---- semantic discovery (Thread D) ----

    def supports_search(self) -> bool:
        return True

    def search(
        self, kind: str, query_text: str, *, name_prefix: str = "", k: int = 10
    ) -> list[tuple[str, float]]:
        """BM25 semantic search over the plain-text companions. Returns
        ``[(record_name, score)]`` ranked by relevance, with the ``.text`` suffix
        stripped so callers get the real record names."""
        import re  # noqa: PLC0415

        blob_re = f"{re.escape(name_prefix)}.*{re.escape(_SEARCH_SUFFIX)}"
        results = self._client.SemanticSearch(
            kind, blob_re, query_text, k, self._cte.PoolQuery.Dynamic()
        )
        out: list[tuple[str, float]] = []
        for r in results:
            bn = r.blob_name
            if bn.endswith(_SEARCH_SUFFIX):
                bn = bn[: -len(_SEARCH_SUFFIX)]
            out.append((bn, float(r.score)))
        return out


# Default clio-core CTE config: a self-managed DRAM↔disk hierarchy on the OS data
# dir. The DRAM tier (score 1.0) is the hot working set; the file tier (score 0.0)
# is the cold spill target. ``restart``/``metadata_log_path``/``transaction_log_capacity``
# are declared so the backend is ready for clio-core's cross-restart data recovery
# when it lands upstream (today that recovery is WIP, so durability rides the file
# trace + rebuild-on-reload — a permanent warm-up step, not a stopgap).
_DEFAULT_CTE_CONFIG_TEMPLATE = """\
runtime:
  num_threads: 4
  conf_dir: {conf_dir}
compose:
  - mod_name: clio_bdev
    pool_name: "ram::chi_default_bdev"
    pool_query: local
    pool_id: "301.0"
    bdev_type: ram
    capacity: "0g"
  - mod_name: clio_cte_core
    pool_name: cte_main
    pool_query: local
    pool_id: "512.0"
    restart: true
    storage:
      - path: "ram::cte_ram_tier"
        bdev_type: "ram"
        capacity_limit: "0g"
        score: 1.0
      - path: "{file_tier}"
        bdev_type: "file"
        capacity_limit: "{file_capacity}"
        score: 0.0
    dpe:
      dpe_type: "max_bw"
    performance:
      metadata_log_path: {metadata_log}
      transaction_log_capacity: "32MB"
"""


def default_cte_config_path() -> str:
    """Seed (if absent) and return the default clio-core CTE config path.

    Lives under the OS data dir (:func:`clio_agent.paths.user_data_dir` ``/cte``) and
    declares a DRAM hot tier + a file cold tier, so ARC's clio-core backend is a
    self-managed memory↔disk hierarchy by default — no LocalFS, no manual config.
    """
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle

    cte_dir = paths.user_data_dir() / "cte"
    cte_dir.mkdir(parents=True, exist_ok=True)
    cfg = cte_dir / "cte.yaml"
    if not cfg.is_file():
        capacity = os.environ.get("CLIO_ARC_CTE_FILE_CAPACITY", "50GB").strip() or "50GB"
        cfg.write_text(
            _DEFAULT_CTE_CONFIG_TEMPLATE.format(
                conf_dir=cte_dir / "conf",
                file_tier=cte_dir / "storage.bin",
                file_capacity=capacity,
                metadata_log=cte_dir / "metadata.log",
            ),
            encoding="utf-8",
        )
    return str(cfg)


def make_arc_store(
    *,
    backend: Optional[str] = None,
    data_dir: "str | Path" = ".clio/agent/arc",
    config_path: str = "",
) -> "ARCStore":
    """Build the ARC persistence backend.

    Selection (first match wins):
        1. explicit ``backend`` arg ("cte" | "local")
        2. env ``CLIO_ARC_STORE`` ("cte" | "local")
        3. default ``"cte"`` (clio-core CTE, in-process; the gold-standard backend)

    FAIL LOUD ([[deliberate-config-fail-loud]]): the backend is a deliberate choice,
    not a silent default. When ``"cte"`` is selected (explicitly or by default) and the
    binding is absent or the runtime fails to init, this RAISES — it does NOT silently
    degrade to ``LocalFSStore`` (which would mask a misconfigured deploy and obscure
    that ARC is no longer on clio-core). ``LocalFSStore`` is used ONLY when ``"local"``
    is selected explicitly (``CLIO_ARC_STORE=local`` or ``backend="local"``).
    """
    choice = (backend or os.environ.get("CLIO_ARC_STORE", "cte")).strip().lower()
    if choice == "local":
        return LocalFSStore(data_dir)
    if choice == "cte":
        cfg = config_path or os.environ.get("CLIO_ARC_STORE_CONFIG", "")
        if not cfg:
            # Prefer a per-workspace clio-core config under ``.clio/core`` if present;
            # otherwise seed/use the default DRAM↔disk hierarchy on the OS data dir.
            from clio_agent import paths  # noqa: PLC0415 - avoid import cycle

            ws_cfg = paths.workspace_core_dir() / "cte.yaml"
            cfg = str(ws_cfg) if ws_cfg.is_file() else default_cte_config_path()
        try:
            return CTEStore(config_path=cfg)
        except Exception as exc:  # noqa: BLE001 - re-raise as a loud, actionable error
            raise RuntimeError(
                "ARC is configured for the clio-core CTE backend but it failed to "
                f"initialize ({exc}). This is a hard error, not a silent fallback: fix "
                "the clio-core install/config, or set CLIO_ARC_STORE=local to "
                "deliberately use the LocalFS backend."
            ) from exc
    raise ValueError(f"unknown CLIO_ARC_STORE {choice!r}; expected 'cte' or 'local'")
