"""Measure the gact server's resident memory versus the number of stored sessions.

This is a *measurement* harness for iowarp/clio-agent#893 (resource-usage
campaign, step 2). It answers one question with numbers instead of assumptions:

    How does the gact server's RSS scale with the count of resident sessions,
    given that ``build_app`` eagerly loads **every** session's message ledger
    into ``app.state.messages`` at boot (``MessageStore.load_all`` — see
    ``src/clio_agent/gact/app.py:1293``)?

Method
------
For each requested session count ``N`` the harness:

1. Synthesises a **temporary** on-disk store (never touches the real ``.clio``):
   a ``sessions.json`` registry plus one ``messages/<sid>.json`` per session,
   each a byte-copy of a real rich template session (default: the 891 run
   ``sess_c7fbe367da29``, 135 parts / ~1.25 MB).
2. Boots the **real** gact server as a uvicorn subprocess pointed at that store
   via ``CLIO_SESSIONS_PATH`` (agent-less: no ``CLIO_LM_PROVIDER``, so RSS
   reflects the message-ledger residency, not an LM/ARC hydration). The server
   runs with ``CLIO_ARC_STORE=local`` so the clio-core CTE daemon is never
   involved.
3. Waits for ``GET /v1/health`` to answer (boot-to-healthy time; the eager
   ``load_all`` runs inside ``build_app`` *before* the port binds, so a healthy
   response means every ledger is already resident).
4. Measures a settled RSS (after a ``GET /v1/sessions``), then fetches
   ``GET /v1/sessions/{sid}/messages`` for up to 10 sessions and measures RSS
   again — testing whether *reading* messages grows residency (it should not:
   the reads are served from the already-resident ``app.state.messages``).
5. Kills the server before the next run.

The harness **fails loudly** if a server never becomes healthy; there are no
silent skips.

Outputs a table (boot behavior, RSS vs N, boot time, post-read RSS) plus the
fitted slopes, and writes the raw measurements to a JSON file.

Backend mode (iowarp/clio-agent#893, owner completion requirement)
------------------------------------------------------------------
The legacy sweep above boots agent-less, so the lazy per-process ``ARCMemory`` is
never constructed and the measurement reflects the message ledger only. To measure
the gact server's RSS *with a real ARC backend attached* — in particular the
clio-core CTE backend, the shipped default — pass ``--backend {local,cte}``:

* the server is booted through this script's own ``--serve-app`` submode (a
  self-exec, so all code stays in one file), which builds the real app, **forces**
  ``ARCMemory`` construction via ``_process_arc`` (attaching/​spawning the shared
  clio-core daemon and loading ``clio_cte_core_ext`` for ``cte``), and then
  **fail-loud asserts** that the store the server actually built is the one that was
  requested — a CTE boot that silently degraded to ``LocalFSStore`` (#897) exits
  non-zero *before* binding the port, so the harness never measures the wrong
  backend (the exact mistake #893's requirement exists to prevent).
* ``--measure-daemon`` (cte only) additionally measures the shared clio-core daemon
  process itself — RSS (working set) and committed memory (Windows private bytes /
  commit charge via ``psutil.memory_full_info``) — at idle and after an
  ARC-exercising write load, then stops the daemon (last-one-out), leaving no
  orphaned listener.

Usage
-----
    uv run python scripts/profile_session_memory.py \
        --template-session .clio/agent/messages/sess_c7fbe367da29.json \
        --counts 0,50,200,500 --port 18800 --out /tmp/893_profile.json

    # #893: gact RSS with the clio-core CTE backend attached (fail-loud on degrade)
    uv run python scripts/profile_session_memory.py --backend cte \
        --counts 0,200 --measure-daemon --out /tmp/893_cte.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psutil

# ----------------------------------------------------------------------------- #
# Measurement record
# ----------------------------------------------------------------------------- #


@dataclass
class RunResult:
    """One (session-count) measurement point.

    Attributes:
        count: Number of resident sessions the server was booted against.
        boot_seconds: Wall time from subprocess spawn to first healthy
            ``GET /v1/health`` response (includes import + eager ``load_all``).
        rss_boot_mb: Settled RSS (MiB) after boot + one ``GET /v1/sessions``.
        rss_after_read_mb: RSS (MiB) after fetching messages for up to 10
            sessions — probes whether reads grow residency.
        rss_baseline_import_mb: RSS (MiB) of the process, recorded at boot; the
            N=0 point serves as the fixed import/framework baseline.
        sessions_listed: Count returned by ``GET /v1/sessions`` (sanity check).
        messages_fetched: Number of sessions whose messages were fetched.
    """

    count: int
    boot_seconds: float
    rss_boot_mb: float
    rss_after_read_mb: float
    sessions_listed: int
    messages_fetched: int
    backend: str = "none"


@dataclass
class DaemonMemory:
    """A memory snapshot of the shared clio-core (``clio_run``) daemon process.

    Windows honesty (``psutil.memory_full_info``): ``rss_mb`` is the working set
    (physically resident pages); ``committed_mb`` is the process private commit
    charge (``pagefile``/``private`` — memory the OS has *committed* backing store
    for, resident or not). The 1 GiB main shared-memory segment shows up in the
    committed figure once it is committed, while ``rss_mb`` reflects only the pages
    actually touched — so the two together tell committed-vs-resident honestly.

    Attributes:
        label: ``"idle"`` or ``"load"``.
        pid: The daemon PID (from ``~/.clio/clio-runtime.pid``).
        rss_mb: Working set / resident set size (MiB).
        committed_mb: Private commit charge (MiB) — Windows ``pagefile``/``private``,
            POSIX falls back to ``vms``/``uss`` (see :func:`_daemon_memory`).
        vms_mb: Total virtual address space (MiB).
        uss_mb: Unique set size (MiB) — memory private to this process.
        num_threads: Daemon thread count (context; the runtime is multi-threaded).
        method: A short description of how ``committed_mb`` was derived on this OS.
    """

    label: str
    pid: int
    rss_mb: float
    committed_mb: float
    vms_mb: float
    uss_mb: float
    num_threads: int
    method: str


# ----------------------------------------------------------------------------- #
# Backend selection + fail-loud assertion (#893)
# ----------------------------------------------------------------------------- #

# The store class name each backend must resolve to. A CTE boot that degraded to
# LocalFS (#897) resolves to "LocalFSStore" here and the assertion below fires.
_EXPECTED_STORE_CLASS: dict[str, str] = {
    "local": "LocalFSStore",
    "cte": "CTEStore",
}


def assert_backend(arc: Any, requested: str) -> str:
    """Fail loud unless ``arc``'s persistence store matches the ``requested`` backend.

    The #893 owner requirement: never let a measurement silently run on the wrong
    backend. ``ARCMemory`` holds its store on ``_store``; we read its concrete class
    name and compare it to the class the requested backend must produce. A CTE boot
    that degraded to :class:`~clio_agent.arc.storage.LocalFSStore` (#897) therefore
    raises here — *before* the server binds its port — instead of being measured as
    if it were CTE.

    Args:
        arc: The constructed ``ARCMemory`` (or any object exposing ``_store``).
        requested: The backend that was asked for (``"local"`` or ``"cte"``).

    Returns:
        The confirmed store class name.

    Raises:
        RuntimeError: If the store class does not match the requested backend, or the
            backend name is unknown / the store is missing.
    """
    expected = _EXPECTED_STORE_CLASS.get(requested)
    if expected is None:
        raise RuntimeError(f"unknown --backend {requested!r}; expected 'local' or 'cte'")
    store = getattr(arc, "_store", None)
    actual = type(store).__name__ if store is not None else "None"
    if actual != expected:
        raise RuntimeError(
            f"ARC backend mismatch: requested={requested!r} expected store {expected!r} "
            f"but the server built {actual!r}. A CTE request that resolves to LocalFSStore "
            "means clio-core failed to init and degraded (#897); the measurement would be "
            "of the WRONG backend. Refusing to serve."
        )
    return actual


def _serve_app(backend: str, port: int) -> None:
    """Self-exec submode: build the real gact app, force+assert ARC, then serve.

    Booting via this script (rather than ``uvicorn clio_agent.gact.app:app``) lets the
    ``--backend`` measurement construct the per-process ``ARCMemory`` eagerly — the
    module-level app is agent-less, so ARC is otherwise never built and no CTE binding
    is loaded. We construct it via ``_process_arc`` (the same choke point the agent
    build uses), assert it is the requested backend (fail loud, exit non-zero before
    the port binds), then hand the app to uvicorn. The parent sets ``CLIO_ARC_STORE``
    and the store env; here we only build + assert + serve.
    """
    import uvicorn  # noqa: PLC0415

    from clio_agent.gact.app import build_app  # noqa: PLC0415
    from clio_agent.gact.runtime.globals import _process_arc  # noqa: PLC0415

    app = build_app()
    arc = _process_arc(app)  # forces make_arc_store(); CTE spawns/attaches the daemon
    confirmed = assert_backend(arc, backend)  # raises -> non-zero exit before bind
    print(f"[profile-serve] ARC backend confirmed: {confirmed} (requested {backend})", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


# ----------------------------------------------------------------------------- #
# Temp store synthesis
# ----------------------------------------------------------------------------- #


def _session_record(sid: str, template_rec: dict[str, Any], msg_count: int) -> dict[str, Any]:
    """Return a valid ``Session`` dict for ``sid`` cloned from the template record."""

    rec = dict(template_rec)
    rec["id"] = sid
    rec["title"] = f"synthetic {sid}"
    rec["message_count"] = msg_count
    return rec


def build_temp_store(
    root: Path,
    count: int,
    template_msg_path: Path,
    template_rec: dict[str, Any],
) -> Path:
    """Materialise a temp store with ``count`` copies of the template session.

    Layout mirrors the real store: ``<root>/sessions.json`` (the registry) and
    ``<root>/messages/<sid>.json`` (one byte-copy of the template per session).

    Args:
        root: Directory to populate (created if absent).
        count: Number of synthetic sessions to write.
        template_msg_path: Path to the real template message ledger to copy.
        template_rec: The template's ``sessions.json`` record to clone.

    Returns:
        The ``sessions.json`` path to pass as ``CLIO_SESSIONS_PATH``.
    """

    messages_dir = root / "messages"
    messages_dir.mkdir(parents=True, exist_ok=True)
    template_bytes = template_msg_path.read_bytes()
    msg_count = len(json.loads(template_bytes))

    registry: dict[str, dict[str, Any]] = {}
    for i in range(count):
        sid = f"sess_synth{i:06d}"
        (messages_dir / f"{sid}.json").write_bytes(template_bytes)
        registry[sid] = _session_record(sid, template_rec, msg_count)

    sessions_path = root / "sessions.json"
    sessions_path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    return sessions_path


# ----------------------------------------------------------------------------- #
# HTTP helpers (stdlib only — no dependency on the running server's client)
# ----------------------------------------------------------------------------- #


def _get_json(url: str, timeout: float = 10.0) -> Any:
    """GET ``url`` and decode JSON, raising on any transport/HTTP error."""

    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - localhost only
        return json.loads(resp.read().decode("utf-8"))


def _wait_healthy(base_url: str, proc: subprocess.Popen[bytes], deadline_s: float) -> float:
    """Block until ``GET /v1/health`` answers; return seconds waited.

    Raises:
        RuntimeError: if the process dies or the deadline elapses before the
            server answers — this harness must FAIL loudly, never skip.
    """

    start = time.monotonic()
    url = f"{base_url}/v1/health"
    while True:
        if proc.poll() is not None:
            raise RuntimeError(
                f"gact server exited early (code={proc.returncode}) before becoming "
                f"healthy at {url}"
            )
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:  # noqa: S310 - localhost
                if resp.status == 200:
                    return time.monotonic() - start
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        if time.monotonic() - start > deadline_s:
            raise RuntimeError(
                f"gact server never became healthy at {url} within {deadline_s:.0f}s"
            )
        time.sleep(0.25)


# ----------------------------------------------------------------------------- #
# RSS measurement
# ----------------------------------------------------------------------------- #


def _process_rss_mb(pid: int) -> float:
    """Return RSS (MiB) of ``pid`` plus all descendants (uvicorn may fork)."""

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return 0.0
    total = proc.memory_info().rss
    for child in proc.children(recursive=True):
        try:
            total += child.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return total / (1024 * 1024)


# ----------------------------------------------------------------------------- #
# One measurement run
# ----------------------------------------------------------------------------- #


def _server_command(backend: str, port: int) -> list[str]:
    """The subprocess argv that boots one measurement server on ``port``.

    ``backend == "none"`` keeps the legacy agent-less path
    (``uvicorn clio_agent.gact.app:app``) that measures message-ledger residency with
    no ARC attached. ``"local"``/``"cte"`` re-exec THIS script's ``--serve-app``
    submode, which forces ``ARCMemory`` construction and fail-loud asserts the backend
    (#893) before binding.
    """
    if backend == "none":
        return [
            sys.executable,
            "-m",
            "uvicorn",
            "clio_agent.gact.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ]
    return [
        sys.executable,
        os.path.abspath(__file__),
        "--serve-app",
        "--backend",
        backend,
        "--port",
        str(port),
    ]


def run_one(
    count: int,
    port: int,
    template_msg_path: Path,
    template_rec: dict[str, Any],
    tmp_root: Path,
    boot_deadline_s: float,
    settle_s: float,
    backend: str = "none",
) -> RunResult:
    """Boot a server against ``count`` synthetic sessions and measure its RSS.

    ``backend`` selects the ARC persistence backend the server boots with: ``"none"``
    (legacy, agent-less, no ARC), ``"local"`` (forced ``LocalFSStore``), or ``"cte"``
    (forced clio-core CTE backend, fail-loud asserted — #893).
    """

    store_root = tmp_root / f"store_{count}"
    if store_root.exists():
        shutil.rmtree(store_root)
    sessions_path = build_temp_store(store_root, count, template_msg_path, template_rec)

    env = dict(os.environ)
    env["CLIO_SESSIONS_PATH"] = str(sessions_path)
    # Backend selection: legacy "none" keeps LocalFS with no ARC construction; the
    # explicit modes set CLIO_ARC_STORE so make_arc_store builds the requested store.
    env["CLIO_ARC_STORE"] = "local" if backend == "none" else backend
    env.pop("CLIO_LM_PROVIDER", None)  # agent-less: isolate ARC/ledger residency (no LM)
    # Allow the temp store + cwd so no file policy trips the boot.
    env["CLIO_ALLOWED_ROOTS"] = os.pathsep.join([str(tmp_root), str(Path.cwd())])

    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        _server_command(backend, port),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        boot_seconds = _wait_healthy(base_url, proc, boot_deadline_s)
        time.sleep(settle_s)  # let the event loop settle before sampling

        sessions = _get_json(f"{base_url}/v1/sessions?include_all_workspaces=true")
        listed = sessions.get("sessions", sessions) if isinstance(sessions, dict) else sessions
        sessions_listed = len(listed) if isinstance(listed, list) else 0

        time.sleep(settle_s)
        rss_boot_mb = _process_rss_mb(proc.pid)

        # Fetch messages for up to 10 sessions — does reading grow residency?
        ids = [row["id"] for row in listed if isinstance(row, dict)][:10] if listed else []
        for sid in ids:
            _get_json(f"{base_url}/v1/sessions/{sid}/messages")
        time.sleep(settle_s)
        rss_after_read_mb = _process_rss_mb(proc.pid)

        return RunResult(
            count=count,
            boot_seconds=round(boot_seconds, 3),
            rss_boot_mb=round(rss_boot_mb, 2),
            rss_after_read_mb=round(rss_after_read_mb, 2),
            sessions_listed=sessions_listed,
            messages_fetched=len(ids),
            backend=backend,
        )
    finally:
        _terminate_server_tree(proc)


def _terminate_server_tree(proc: subprocess.Popen[bytes]) -> None:
    """Tree-kill the spawned gact server and every descendant (#900 harness discipline).

    The booted server fans out into MCP stdio children + a pooled SDK CLI process; a
    plain ``proc.terminate()`` on the parent orphans them (on Windows terminating the
    parent never reaps the tree). Reuse the audited
    :func:`clio_agent.serve._terminate_tree` (psutil-recursive + POSIX process-group)
    so the whole tree is reaped between runs.
    """
    from clio_agent.serve import _terminate_tree  # noqa: PLC0415

    _terminate_tree(proc.pid, record_create_time=None, trusted=True)


# ----------------------------------------------------------------------------- #
# clio-core daemon (clio_run) memory measurement (#893, component 2)
# ----------------------------------------------------------------------------- #


def _daemon_pid() -> int | None:
    """Return the shared clio-core daemon PID from ``~/.clio/clio-runtime.pid``."""
    try:
        parts = (Path.home() / ".clio" / "clio-runtime.pid").read_text("utf-8").split()
    except OSError:
        return None
    return int(parts[0]) if parts else None


def _daemon_memory(label: str, pid: int) -> DaemonMemory:
    """Snapshot the daemon's resident + committed memory (OS-honest — see DaemonMemory).

    On Windows ``memory_full_info`` exposes ``private``/``pagefile`` (the process's
    committed private bytes) distinctly from ``wset``/``rss`` (working set); we report
    ``committed_mb`` from the private commit charge so a committed-but-not-resident
    shared-memory segment is not mistaken for freed memory. On POSIX (no ``private``
    field) we fall back to ``uss`` for the committed figure and name the method so the
    number is never silently apples-to-oranges.
    """
    proc = psutil.Process(pid)
    mfi = proc.memory_full_info()
    fields = set(mfi._fields)
    rss = float(mfi.rss)
    vms = float(mfi.vms)
    uss = float(getattr(mfi, "uss", 0.0))
    if "private" in fields:  # Windows: private commit charge
        committed = float(mfi.private)
        method = "win:memory_full_info.private (committed private bytes)"
    elif "pagefile" in fields:
        committed = float(mfi.pagefile)
        method = "win:memory_full_info.pagefile (commit charge)"
    else:  # POSIX
        committed = uss or vms
        method = "posix:memory_full_info.uss (unique set size)"
    return DaemonMemory(
        label=label,
        pid=pid,
        rss_mb=round(rss / (1024 * 1024), 2),
        committed_mb=round(committed / (1024 * 1024), 2),
        vms_mb=round(vms / (1024 * 1024), 2),
        uss_mb=round(uss / (1024 * 1024), 2),
        num_threads=proc.num_threads(),
        method=method,
    )


def measure_daemon(load_ops: int, load_blob_bytes: int, settle_s: float) -> list[DaemonMemory]:
    """Measure the shared clio-core daemon at idle and after an ARC write load.

    Owns the daemon lifecycle for the measurement (#893 discipline): constructs one
    in-process ``CTEStore`` (this process becomes a client, spawning the shared daemon
    if none is up — FAIL LOUD if it never binds), snapshots the daemon **idle**, drives
    ``load_ops`` blob writes through the store (the ARC-exercising workload), snapshots
    it **under load**, clears the test blobs, and releases the client last-one-out so
    the daemon is stopped and no orphaned listener remains.

    Returns the ``[idle, load]`` snapshots (may be shorter if the daemon PID is
    unreadable at a step — surfaced by the caller, never silently skipped).
    """
    from clio_agent.arc import storage as arc_storage  # noqa: PLC0415
    from clio_agent.arc.storage import make_arc_store  # noqa: PLC0415

    snaps: list[DaemonMemory] = []
    store = make_arc_store(backend="cte", data_dir=".clio/agent/arc")
    if type(store).__name__ != "CTEStore":  # fail loud: the daemon measurement needs CTE
        raise RuntimeError(
            f"measure-daemon requires the CTE backend but got {type(store).__name__}; "
            "clio-core failed to init (#897). Cannot measure the daemon."
        )
    try:
        time.sleep(settle_s)
        pid = _daemon_pid()
        if pid is None:
            raise RuntimeError("clio-core daemon pidfile is empty/absent; cannot measure it.")
        snaps.append(_daemon_memory("idle", pid))

        blob = b"x" * load_blob_bytes
        for i in range(load_ops):
            store.put("segments", f"profile_daemon_load_{i:06d}", blob)
        time.sleep(settle_s)
        snaps.append(_daemon_memory("load", pid))

        # Clean the test blobs we wrote (leave the store as we found it).
        for i in range(load_ops):
            store.delete("segments", f"profile_daemon_load_{i:06d}")
    finally:
        # Last-one-out stop: releases the daemon since this is the only live client.
        arc_storage.release_runtime_client("", "error")
        time.sleep(settle_s)
        if arc_storage._runtime_alive(arc_storage._resolve_runtime_port("")):
            # The clean stop did not free the port; force the pidfile kill (no orphan).
            arc_storage._kill_daemon_pidfile()
    return snaps


def _print_daemon_table(snaps: list[DaemonMemory]) -> None:
    """Print the daemon idle-vs-load memory table (#893 component 2)."""
    if not snaps:
        return
    print()
    print("clio-core daemon (clio_run) memory — component 2:")
    header = (
        f"{'phase':>6} {'pid':>7} {'RSS_MiB':>10} {'committed_MiB':>15} "
        f"{'vms_MiB':>10} {'uss_MiB':>10} {'threads':>8}"
    )
    print(header)
    print("-" * len(header))
    for s in snaps:
        print(
            f"{s.label:>6} {s.pid:>7} {s.rss_mb:>10.2f} {s.committed_mb:>15.2f} "
            f"{s.vms_mb:>10.2f} {s.uss_mb:>10.2f} {s.num_threads:>8}"
        )
    print(f"committed method: {snaps[0].method}")
    if len(snaps) >= 2:
        idle, load = snaps[0], snaps[1]
        print(
            f"idle->load delta: RSS {load.rss_mb - idle.rss_mb:+.2f} MiB, "
            f"committed {load.committed_mb - idle.committed_mb:+.2f} MiB "
            "(the 1 GiB main shm segment commits lazily on first CTE data op)"
        )


# ----------------------------------------------------------------------------- #
# Slope + reporting
# ----------------------------------------------------------------------------- #


def _slope_bytes_per_session(points: list[tuple[int, float]]) -> float:
    """Least-squares slope (bytes/session) of RSS-MiB vs session count."""

    if len(points) < 2:
        return 0.0
    n = len(points)
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0
    slope_mib_per_session = (n * sxy - sx * sy) / denom
    return slope_mib_per_session * 1024 * 1024


def _print_table(results: list[RunResult], template_bytes: int, template_parts: int) -> None:
    """Print the RSS table + fitted slopes to stdout."""

    print()
    print(f"Template session: {template_parts} parts, {template_bytes:,} bytes on disk")
    print()
    header = (
        f"{'N':>6} {'boot_s':>8} {'RSS_boot_MiB':>14} {'RSS_read_MiB':>14} "
        f"{'read_dMiB':>10} {'fetched':>8} {'listed':>7}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        read_delta = r.rss_after_read_mb - r.rss_boot_mb
        print(
            f"{r.count:>6} {r.boot_seconds:>8.2f} {r.rss_boot_mb:>14.2f} "
            f"{r.rss_after_read_mb:>14.2f} {read_delta:>10.2f} "
            f"{r.messages_fetched:>8} {r.sessions_listed:>7}"
        )

    boot_points = [(r.count, r.rss_boot_mb) for r in results]
    boot_slope = _slope_bytes_per_session(boot_points)
    print()
    print(f"RSS-at-boot slope:        {boot_slope:>12,.0f} bytes/session (resident-at-boot cost)")
    # The read probe fetches only min(N, 10) sessions, so the read cost is a
    # per-FETCHED-session figure, not a per-N slope. Average it over the runs
    # that actually fetched something.
    read_costs = [
        ((r.rss_after_read_mb - r.rss_boot_mb) * 1024 * 1024) / r.messages_fetched
        for r in results
        if r.messages_fetched > 0
    ]
    if read_costs:
        avg_read = sum(read_costs) / len(read_costs)
        print(f"read delta per fetched:   {avg_read:>12,.0f} bytes/fetched-session (transient)")
    base = next((r.rss_boot_mb for r in results if r.count == 0), None)
    if base is not None:
        print(f"import/framework baseline (N=0): {base:.2f} MiB")


# ----------------------------------------------------------------------------- #
# Entry point
# ----------------------------------------------------------------------------- #


def main() -> None:
    """Parse args, run the sweep, print the table, and write JSON."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--template-session",
        type=Path,
        default=Path(".clio/agent/messages/sess_c7fbe367da29.json"),
        help="Path to a real message ledger to clone as the template session.",
    )
    parser.add_argument(
        "--template-sessions-json",
        type=Path,
        default=Path(".clio/agent/sessions.json"),
        help="sessions.json holding the template's registry record.",
    )
    parser.add_argument(
        "--counts",
        default="0,50,200,500",
        help="Comma-separated session counts to sweep.",
    )
    parser.add_argument("--port", type=int, default=18800, help="Base port (18800+ range).")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("session_memory_profile.json"),
        help="Where to write raw measurements as JSON.",
    )
    parser.add_argument(
        "--tmp-root",
        type=Path,
        default=None,
        help="Directory for temp stores (default: a sibling of --out).",
    )
    parser.add_argument("--boot-deadline", type=float, default=120.0)
    parser.add_argument("--settle", type=float, default=1.5)
    parser.add_argument(
        "--backend",
        choices=["none", "local", "cte"],
        default="none",
        help=(
            "ARC backend the measured server boots with (#893): 'none' = legacy "
            "agent-less (no ARC); 'local' = forced LocalFSStore; 'cte' = forced "
            "clio-core CTE backend (fail-loud asserted). local/cte force ARCMemory "
            "construction so the delta isolates the CTE binding overhead."
        ),
    )
    parser.add_argument(
        "--serve-app",
        action="store_true",
        help="INTERNAL self-exec: build + assert + serve one backend on --port (not for direct use).",
    )
    parser.add_argument(
        "--measure-daemon",
        action="store_true",
        help="Also measure the shared clio-core daemon (idle vs load) — cte backend only (#893).",
    )
    parser.add_argument("--daemon-load-ops", type=int, default=100, help="Daemon-load blob writes.")
    parser.add_argument(
        "--daemon-load-kb", type=int, default=1250, help="Per-blob size (KiB) for the daemon load."
    )
    args = parser.parse_args()

    # INTERNAL self-exec submode: this process IS one measured server (#893). Build the
    # real app, force+assert the requested ARC backend, then serve until killed.
    if args.serve_app:
        if args.backend not in ("local", "cte"):
            raise SystemExit("--serve-app requires --backend local|cte")
        _serve_app(args.backend, args.port)
        return

    template_msg_path = args.template_session.resolve()
    if not template_msg_path.exists():
        raise SystemExit(f"template session not found: {template_msg_path}")
    template_bytes = template_msg_path.read_bytes()
    template_size = len(template_bytes)
    template_parts = sum(len(m.get("parts", [])) for m in json.loads(template_bytes))

    registry = json.loads(args.template_sessions_json.read_text(encoding="utf-8"))
    template_sid = template_msg_path.stem
    template_rec = registry.get(template_sid) or next(iter(registry.values()))

    tmp_root = args.tmp_root or (args.out.resolve().parent / "_session_mem_stores")
    tmp_root.mkdir(parents=True, exist_ok=True)

    if args.measure_daemon and args.backend != "cte":
        raise SystemExit("--measure-daemon requires --backend cte")

    counts = [int(c) for c in args.counts.split(",") if c.strip()]
    results: list[RunResult] = []
    try:
        for i, count in enumerate(counts):
            port = args.port + i
            print(f"[run] backend={args.backend} N={count} port={port} ...", flush=True)
            results.append(
                run_one(
                    count=count,
                    port=port,
                    template_msg_path=template_msg_path,
                    template_rec=template_rec,
                    tmp_root=tmp_root,
                    boot_deadline_s=args.boot_deadline,
                    settle_s=args.settle,
                    backend=args.backend,
                )
            )
    finally:
        # Clean up the (potentially large) synthetic stores.
        shutil.rmtree(tmp_root, ignore_errors=True)

    _print_table(results, template_size, template_parts)

    daemon_snaps: list[DaemonMemory] = []
    if args.measure_daemon:
        print("\n[daemon] measuring shared clio-core daemon (idle -> load) ...", flush=True)
        daemon_snaps = measure_daemon(
            load_ops=args.daemon_load_ops,
            load_blob_bytes=args.daemon_load_kb * 1024,
            settle_s=args.settle,
        )
        _print_daemon_table(daemon_snaps)

    payload = {
        "template": {
            "path": str(template_msg_path),
            "bytes": template_size,
            "parts": template_parts,
        },
        "backend": args.backend,
        "runs": [asdict(r) for r in results],
        "daemon": [asdict(s) for s in daemon_snaps],
    }
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
