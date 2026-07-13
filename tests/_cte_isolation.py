"""Hermetic per-suite clio-core daemon isolation (the daemon-ghost fix, PART A).

WHY. The suite's ``cte``-leg tests used to attach to the HOST-SHARED clio-core daemon
(port 9413) — the same instance live CLIO servers on this machine use. Two failure
modes followed:

* **accretion** — every suite run dumped its working sets / stress corpora into the
  shared daemon's tiers with no cleanup (the 12.3 GiB resident daemon the owner found);
* **cross-instance flakes** — a live server writing ``segments`` concurrently with a
  suite's BM25 search or a size-then-read ``get`` perturbs corpus statistics / truncates
  reads (observed: ``test_auto_compaction[cte]`` msgspec truncation,
  ``test_working_set_fold_search[cte]`` ranking divergence — each green standalone).

WHAT. :func:`isolate_cte_env` points the pytest process at a PRIVATE daemon: a private
config (own contiguous port block, own storage/metadata paths, bounded tiers) plus
``CLIO_RUNTIME_STATE_DIR`` (see :func:`clio_agent.arc.clio_core_config.runtime_state_dir`)
so the spawn lock, pidfile, client registry, and daemon log all live under the session
tmp dir. The daemon is spawned lazily by the normal connect-or-spawn path on the first
``cte`` attach, and stopped by the normal last-one-out release at session end (the
process-hygiene fixture calls it deterministically); :func:`reap_private_daemon` is the
belt-and-suspenders kill for a crashed run. Nothing here touches the host's ``~/.clio``.

Non-cte tests are unaffected: the env only redirects clio-core coordination paths, and
tests default to ``CLIO_ARC_STORE=local`` anyway.
"""

from __future__ import annotations

import random
import socket
from dataclasses import dataclass
from pathlib import Path

# Private clio-core topology for the suite: DRAM hot tier + file cold tier, own port.
# Mirrors the proven private-daemon config of tests/test_arc/test_clio_core_offload_spill.py
# with suite-sized caps (the offload test keeps its own tiny 2MB cap to force spill).
_PRIVATE_CONFIG_TEMPLATE = """\
networking:
  port: {port}
runtime:
  num_threads: 2
  conf_dir: "{conf_dir}"
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
        capacity_limit: "256MB"
        score: 1.0
      - path: "{file_tier}"
        bdev_type: "file"
        capacity_limit: "4GB"
        score: 0.0
    dpe:
      dpe_type: "max_bw"
    performance:
      metadata_log_path: "{metadata_log}"
      transaction_log_capacity: "32MB"
"""


@dataclass(frozen=True)
class CteIsolation:
    """The private-daemon environment prepared for this suite run."""

    state_dir: Path  # CLIO_RUNTIME_STATE_DIR (lock/pidfile/registry/log)
    config_path: Path  # the private cte.yaml
    port: int  # base of the reserved contiguous port block


def cte_isolation_available() -> bool:
    """True when the clio-core binding AND standalone launcher are present.

    Without the binding no ``cte``-leg test runs (they ``importorskip``), and without
    the launcher no private daemon can spawn — in either case the env is left untouched
    (the status quo, not a silent degrade: the cte legs skip or fail on their own terms).
    """
    try:
        import clio_cte_core_ext  # noqa: F401, PLC0415
        import iowarp_core  # noqa: PLC0415
    except ImportError:
        return False
    from clio_agent.arc import storage  # noqa: PLC0415

    return storage._runtime_launcher_path(iowarp_core) is not None


def reserve_port_block(block: int = 5) -> int:
    """Return a base port with ``block`` consecutive free ports, below the ephemeral range.

    clio-core binds a CONTIGUOUS cluster of ports around ``networking.port``; a base in
    the ephemeral range risks base+N colliding with a transient connection, half-binding
    the daemon. Same approach as the offload-spill test.
    """
    for _ in range(400):
        base = random.randint(20000, 40000)  # noqa: S311 - port pick, not crypto
        socks: list[socket.socket] = []
        ok = True
        for off in range(block):
            sock = socket.socket()
            try:
                sock.bind(("0.0.0.0", base + off))  # noqa: S104 - bind test, released below
                socks.append(sock)
            except OSError:
                ok = False
                break
        for sock in socks:
            sock.close()
        if ok:
            return base
    raise RuntimeError("could not reserve a free contiguous port block for the private daemon")


def isolate_cte_env(root: Path, environ: dict[str, str]) -> CteIsolation:
    """Write the private config under ``root`` and set the isolation env in ``environ``.

    Sets ``CLIO_RUNTIME_STATE_DIR`` (coordination state), ``CLIO_ARC_STORE_CONFIG`` +
    ``CLIO_SERVER_CONF`` (both the client's store init and the spawned daemon compose
    the private tiers), and ``CLIO_CORE_PORT`` (liveness probes match the private bind).

    Args:
        root: Session-scoped directory to hold state/config/storage.
        environ: The environment mapping to mutate (``os.environ`` in conftest).

    Returns:
        The prepared :class:`CteIsolation` facts.
    """
    state_dir = root / "clio-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    conf_dir = root / "conf"
    conf_dir.mkdir(exist_ok=True)
    store_dir = root / "store"
    store_dir.mkdir(exist_ok=True)

    port = reserve_port_block()
    config_path = root / "cte.yaml"
    config_path.write_text(
        _PRIVATE_CONFIG_TEMPLATE.format(
            port=port,
            conf_dir=conf_dir.as_posix(),
            file_tier=(store_dir / "storage.bin").as_posix(),
            metadata_log=(store_dir / "metadata.log").as_posix(),
        ),
        encoding="utf-8",
    )

    environ["CLIO_RUNTIME_STATE_DIR"] = str(state_dir)
    environ["CLIO_ARC_STORE_CONFIG"] = str(config_path)
    environ["CLIO_SERVER_CONF"] = str(config_path)
    environ["CLIO_CORE_PORT"] = str(port)
    return CteIsolation(state_dir=state_dir, config_path=config_path, port=port)


def eagerly_attach_private_daemon() -> bool:
    """Spawn the private daemon + attach this process's client NOW (session start).

    Lazy spawn-on-first-attach worked, but it put the one fragile step (daemon boot,
    port bind) in the middle of suite load where a transient failure degrades every
    later cte leg. Attaching eagerly at session start makes the spawn deterministic
    and keeps the daemon alive for the whole session (this process is a registered
    client until the session-end release stops it, last one out).

    Returns:
        True when the clio-core backend came up; False when it degraded (already
        recorded LOUDLY by :mod:`clio_agent.arc.init_degradation` — nothing is hidden,
        the cte legs will then fail/degrade on their own terms).
    """
    from clio_agent.arc.storage import ClioCoreStore, make_arc_store  # noqa: PLC0415

    store = make_arc_store(backend="cte")
    return isinstance(store, ClioCoreStore)


def reap_private_daemon(state_dir: Path) -> None:
    """Force-reap the private daemon via its pidfile (belt-and-suspenders, best-effort).

    The normal path is the last-one-out ``release_runtime_client`` stop; this covers a
    run whose clean stop failed. Only ever touches the PRIVATE state dir. PID-reuse
    guarded via psutil create-time-free existence (the pidfile lives in a dir created
    this session, so reuse across the session is the only residual risk — acceptable
    for a kill fallback).
    """
    pidfile = state_dir / "clio-runtime.pid"
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
    try:
        import psutil  # noqa: PLC0415

        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except psutil.TimeoutExpired:
            proc.kill()
    except Exception:  # noqa: BLE001,S110 - already gone / psutil missing: best-effort kill
        pass
