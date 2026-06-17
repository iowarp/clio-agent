"""Worker-fleet orchestration for the isolated detached delegation model (epic #667).

The topology/orchestration layer the multinode path needs: bring up N isolated workers
per role over a shared store, keep them alive (supervise → respawn on process death), and
tear them down cleanly. The parent never talks to this layer — it routes to workers via
:class:`~clio_agent.runtime.clio_core_transport.IsolatedExpertInvoker`, which discovers
them by presence heartbeat. This layer only MANAGES the worker processes.

The spawner is a seam, not a hardcode. :class:`LocalSubprocessSpawner` runs workers as
local OS processes here (single box, or many boxes each running their own fleet); a cluster
scheduler that places one worker per node (``srun``/``mpirun``/k8s) implements the same
:class:`Spawner` protocol and drops in without touching the parent, the transport, or the
worker code. Either way a worker is the standard isolated entrypoint
(``python -m clio_agent.runtime.clio_core_worker`` with ``CLIO_CORE_ISOLATED=1``).

Single box, two data workers + one analysis worker over a shared LocalFS store::

    fleet = WorkerFleet(
        store,
        [WorkerSpec("data", replicas=2), WorkerSpec("analysis", replicas=1)],
        spawner=LocalSubprocessSpawner(),
        worker_env=localfs_worker_env(store),
    )
    fleet.start()                       # spawn all, wait until every role is present
    try:
        ...                             # parent delegates via IsolatedExpertInvoker(role=...)
    finally:
        fleet.stop()

On a cluster the only change is the store env (``cte_worker_env()`` to attach the workers to
the node's ``clio_run`` daemon) and the spawner (a node-placing one). The parent code, the
mailbox transport, and the worker are identical.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from clio_agent.runtime.clio_core_transport import drop_presence, live_workers

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerSpec:
    """One role's desired capacity: ``replicas`` isolated workers all draining role ``role``."""

    role: str
    replicas: int = 1

    def __post_init__(self) -> None:
        if not self.role:
            raise ValueError("WorkerSpec.role must be non-empty")
        if self.replicas < 1:
            raise ValueError(f"WorkerSpec.replicas must be >= 1 (got {self.replicas})")


def parse_fleet_spec(spec: str) -> list[WorkerSpec]:
    """Parse a ``"role:replicas,role:replicas"`` string (e.g. ``"data:2,analysis:1"``) into
    :class:`WorkerSpec`s. A bare ``role`` means one replica. Whitespace/empty entries are
    ignored. Raises ``ValueError`` on a malformed entry so a typo fails loudly, not silently."""
    specs: list[WorkerSpec] = []
    for raw in spec.split(","):
        entry = raw.strip()
        if not entry:
            continue
        if ":" in entry:
            role, _, count = entry.partition(":")
            role = role.strip()
            try:
                replicas = int(count.strip())
            except ValueError as exc:
                raise ValueError(f"bad replica count in fleet spec entry {entry!r}") from exc
        else:
            role, replicas = entry, 1
        specs.append(WorkerSpec(role=role, replicas=replicas))
    return specs


@runtime_checkable
class Spawner(Protocol):
    """Launches and reaps one isolated worker. The seam between *deciding the fleet* (this
    module) and *placing a worker* (local subprocess here; a cluster scheduler elsewhere)."""

    def spawn(self, *, role: str, worker_id: str, env: Mapping[str, str]) -> Any:
        """Start a worker for ``role`` identified by ``worker_id`` with ``env`` merged onto the
        ambient environment. Return an opaque handle this spawner understands."""
        ...

    def is_alive(self, handle: Any) -> bool:
        """Whether the worker behind ``handle`` is still running (process not yet exited)."""
        ...

    def terminate(self, handle: Any, *, timeout: float = 10.0) -> None:
        """Stop the worker behind ``handle``, escalating to a hard kill after ``timeout``."""
        ...


class LocalSubprocessSpawner:
    """Run each isolated worker as a local OS process.

    Default command is the standard isolated entrypoint; pass ``command`` to point at a custom
    one (e.g. a test entry that registers ad-hoc experts). ``start_new_session=True`` makes each
    worker a process-group leader so :meth:`terminate` takes down its whole tree, never an
    orphan. With ``log_dir`` set, each worker's stdout+stderr stream to
    ``<log_dir>/<role>.<worker_id>.log`` for postmortem.
    """

    def __init__(self, command: Optional[list[str]] = None, *, log_dir: Optional[str] = None) -> None:
        self._command = list(command) if command else [
            sys.executable,
            "-m",
            "clio_agent.runtime.clio_core_worker",
        ]
        self._log_dir = log_dir

    def spawn(self, *, role: str, worker_id: str, env: Mapping[str, str]) -> Any:
        log = None
        stdout = None
        if self._log_dir:
            os.makedirs(self._log_dir, exist_ok=True)
            log = open(os.path.join(self._log_dir, f"{role}.{worker_id}.log"), "w")  # noqa: SIM115
            stdout = log
        try:
            proc = subprocess.Popen(
                self._command,
                env={**os.environ, **dict(env)},
                stdout=stdout,
                stderr=subprocess.STDOUT if stdout is not None else None,
                start_new_session=True,  # own session/group: pgid == pid, killable as a tree
            )
        except BaseException:
            # Popen can raise (fork EAGAIN/ENOMEM, fd exhaustion, bad command). Close the log we
            # just opened so a failed/retried spawn doesn't leak an fd per attempt.
            if log is not None:
                with contextlib.suppress(Exception):
                    log.close()
            raise
        # start_new_session makes the child its own process-group leader, so pgid == pid.
        # Capture it at spawn so terminate can kill the group EVEN AFTER the leader exited
        # (a crashed leader can leave live grandchildren that getpgid-after-reap couldn't find).
        return (proc, log, proc.pid)

    def is_alive(self, handle: Any) -> bool:
        proc = handle[0]
        return proc.poll() is None

    def terminate(self, handle: Any, *, timeout: float = 10.0) -> None:
        proc, log, pgid = handle
        try:
            if proc.poll() is None:
                # graceful-then-hard on the whole group
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        proc.kill()
                    with contextlib.suppress(Exception):
                        proc.wait(timeout=timeout)
            # ALWAYS sweep the group, even if the leader already exited: supervise_once calls
            # terminate() precisely on DEAD leaders, and a crashed worker can leave live
            # grandchildren (its MCP stdio servers) in the group. The alive-only branch above
            # would skip them, orphaning a tree to init that piles up across respawns. killpg on
            # an empty/gone group raises ESRCH → suppressed (the group stays valid while any
            # member lives, so the pgid is not recycled out from under us).
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(pgid, signal.SIGKILL)
        finally:
            if log is not None:
                with contextlib.suppress(Exception):
                    log.close()


def localfs_worker_env(store: Any) -> dict[str, str]:
    """Env that points a worker at the SAME LocalFS directory the parent's ``store`` uses, so
    parent and workers share one mailbox. Raises if ``store`` is not a LocalFS store (a CTE/
    cluster deployment must pass :func:`cte_worker_env` or its own attach env explicitly)."""
    data_dir = getattr(store, "data_dir", None)
    if data_dir is None:
        raise ValueError(
            "store has no data_dir (not a LocalFSStore); pass worker_env explicitly — e.g. "
            "cte_worker_env() to attach workers to a shared clio_run daemon"
        )
    return {"CLIO_ARC_STORE": "local", "CLIO_ARC_DATA_DIR": str(data_dir)}


def cte_worker_env(config_path: str = "") -> dict[str, str]:
    """Env that attaches a worker to the node's shared ``clio_run`` daemon (the cluster
    transport): ``CLIO_ARC_STORE=cte`` + ``CLIO_CTE_WITH_RUNTIME=0`` (attach, don't embed)."""
    env = {"CLIO_ARC_STORE": "cte", "CLIO_CTE_WITH_RUNTIME": "0"}
    if config_path:
        env["CLIO_ARC_STORE_CONFIG"] = config_path
    return env


class WorkerFleet:
    """Manage a fleet of isolated workers: spawn the desired replicas per role, keep them
    present (respawn a slot whose process died), and tear them all down.

    Slots are stable (``<role>-<index>``) so a respawn reuses the id — presence stays
    1:1 with desired capacity and the parent's rotation isn't churned. The fleet only owns
    process lifecycle; routing/exactly-once live in the transport.
    """

    def __init__(
        self,
        store: Any,
        specs: Iterable[WorkerSpec],
        *,
        spawner: Spawner,
        worker_env: Mapping[str, str],
        prefix: str = "clio_core_",
        presence_ttl: float = 6.0,
    ) -> None:
        self._store = store
        self._specs = list(specs)
        if not self._specs:
            raise ValueError("WorkerFleet needs at least one WorkerSpec")
        self._spawner = spawner
        self._worker_env = dict(worker_env)
        self._prefix = prefix
        self._presence_ttl = presence_ttl
        self._slots: dict[str, str] = {}  # worker_id -> role (the desired fleet)
        self._handles: dict[str, Any] = {}  # worker_id -> spawner handle

    def _slot_ids(self) -> Iterator[tuple[str, str]]:
        for spec in self._specs:
            for i in range(spec.replicas):
                yield spec.role, f"{spec.role}-{i}"

    def desired_counts(self) -> dict[str, int]:
        """Role → desired replica count (what the fleet is configured to keep running)."""
        counts: dict[str, int] = {}
        for spec in self._specs:
            counts[spec.role] = counts.get(spec.role, 0) + spec.replicas
        return counts

    def live_counts(self) -> dict[str, int]:
        """Role → number of workers currently announcing a fresh presence heartbeat."""
        return {
            role: len(live_workers(self._store, role, prefix=self._prefix, ttl=self._presence_ttl))
            for role in self.desired_counts()
        }

    def _env_for(self, role: str, worker_id: str) -> dict[str, str]:
        return {
            **self._worker_env,
            "CLIO_CORE_ISOLATED": "1",
            "CLIO_CORE_ROLE": role,
            "CLIO_CORE_WORKER_ID": worker_id,
            "CLIO_CORE_PREFIX": self._prefix,
        }

    def _spawn_slot(self, role: str, worker_id: str) -> None:
        self._slots[worker_id] = role
        self._handles[worker_id] = self._spawner.spawn(
            role=role, worker_id=worker_id, env=self._env_for(role, worker_id)
        )

    def start(self, *, wait_ready: bool = True, timeout: float = 120.0, poll: float = 0.1) -> None:
        """Spawn every slot. With ``wait_ready`` (default) block until each role's presence
        count reaches its desired count, raising :class:`TimeoutError` with diagnostics if not.

        If a later slot fails to spawn (or readiness times out), the already-spawned workers are
        torn down before re-raising — a partial start never orphans live worker processes."""
        try:
            for role, worker_id in self._slot_ids():
                self._spawn_slot(role, worker_id)
            if wait_ready:
                self.wait_ready(timeout=timeout, poll=poll)
        except BaseException:
            self.stop()
            raise

    async def wait_ready_async(self, *, timeout: float = 120.0, poll: float = 0.1) -> None:
        """Async form of :meth:`wait_ready` (does not block the event loop)."""
        waited = 0.0
        while True:
            if self._all_present():
                return
            if waited >= timeout:
                raise TimeoutError(
                    f"fleet not ready in {timeout}s: live={self.live_counts()} "
                    f"desired={self.desired_counts()}"
                )
            await asyncio.sleep(poll)
            waited += poll

    def wait_ready(self, *, timeout: float = 120.0, poll: float = 0.1) -> None:
        """Block (sync) until every role reaches its desired presence count, supervising
        crashed slots while waiting; raise :class:`TimeoutError` with diagnostics on expiry."""
        import time  # noqa: PLC0415 - local to avoid a module-time Date/clock import elsewhere

        deadline = time.monotonic() + timeout
        last_supervise = 0.0
        while True:
            now = time.monotonic()
            # Respawn crashed slots, but at a throttled cadence (~1/s) decoupled from ``poll``.
            # Otherwise a worker that crash-loops on startup would be re-spawned every ``poll``
            # (0.1s → ~1200 Popen over a 120s wait), a fork/exec storm.
            if now - last_supervise >= 1.0:
                self.supervise_once()
                last_supervise = now
            if self._all_present():
                return
            if now >= deadline:
                raise TimeoutError(
                    f"fleet not ready in {timeout}s: live={self.live_counts()} "
                    f"desired={self.desired_counts()}"
                )
            time.sleep(poll)

    def _all_present(self) -> bool:
        live = self.live_counts()
        return all(live.get(role, 0) >= want for role, want in self.desired_counts().items())

    def supervise_once(self) -> int:
        """Respawn any slot whose worker PROCESS has exited (crash/OOM/kill). Returns how many
        were respawned. Process-exit is the unambiguous death signal; a process that is alive
        but stopped heartbeating is handled by the invoker's reassign-on-timeout, not here."""
        respawned = 0
        for worker_id, role in list(self._slots.items()):
            handle = self._handles.get(worker_id)
            if handle is not None and self._spawner.is_alive(handle):
                continue
            if handle is not None:
                with contextlib.suppress(Exception):
                    self._spawner.terminate(handle, timeout=1.0)
            # A crashed worker never ran its own drop_presence, so its (now stale) presence blob
            # can still read "live" for up to presence_ttl. Clear it before respawn so the slot
            # isn't advertised — and routed to — until the FRESH process heartbeats and is
            # actually draining its queue.
            with contextlib.suppress(Exception):
                drop_presence(self._store, role, worker_id, prefix=self._prefix)
            self._spawn_slot(role, worker_id)
            respawned += 1
        return respawned

    async def supervise_forever(self, *, stop: asyncio.Event, interval: float = 1.0) -> None:
        """Respawn dead slots until ``stop`` is set (the long-running keep-the-fleet-alive loop).
        A failing supervise tick is logged and swallowed so one bad sweep never kills the loop —
        the fleet must keep supervising even if a single respawn/terminate throws."""
        try:
            while not stop.is_set():
                try:
                    self.supervise_once()
                except Exception:
                    logger.exception("worker-fleet supervise tick failed; continuing")
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise

    def stop(self, *, timeout: float = 10.0) -> None:
        """Terminate every worker and forget the fleet (idempotent)."""
        for handle in list(self._handles.values()):
            with contextlib.suppress(Exception):
                self._spawner.terminate(handle, timeout=timeout)
        self._handles.clear()
        self._slots.clear()

    def __enter__(self) -> "WorkerFleet":
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.stop()
