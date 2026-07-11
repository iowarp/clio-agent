"""Acceptance: the clio-core CTE backend transparently OFFLOADS context data from
its RAM tier to its disk tier under capacity pressure and RELOADS it byte-identical
-- proven through clio-agent's REAL store path (``arc/storage.py`` ``CTEStore``),
not raw binding calls.

WHAT IS PROVEN
    1. OFFLOAD (physical): a ~30 MB working set written into a 2 MB ram-tier
       ``capacity_limit`` forces the backend to spill cold blobs to its disk
       backing file (``storage.bin_node0``). We prove the spill *physically* by
       finding the payload's marker bytes inside that file -- not by a nominal
       placement score (``GetBlobScore`` stays 1.0 after eviction, so it is NOT a
       residency signal; the byte scan is).
    2. RELOAD (transparent): every blob read back through the same store is
       byte-identical to what was written -- the backend rehydrates from disk
       transparently.
    3. NOT VACUOUS: the offload assertion is the marker's *presence on disk*. With
       no capacity pressure the marker is absent (validated out-of-band by raising
       the ram cap; see the module ``sabotage`` note below), so the test cannot
       pass green without a real spill having occurred.

HERMETICITY (why a private daemon in a subprocess)
    clio-core's runtime is a host-global singleton with a one-init-per-process
    guard, and a shared daemon on the default port. To exercise a *small* ram cap
    without disturbing either, this test stands up a DEDICATED daemon: a private
    clio-core config (own ``conf_dir``, own storage path, 2 MB ram
    ``capacity_limit``) on a DISTINCT, contiguous RPC port block, under a private
    ``HOME`` so its runtime bookkeeping (pidfile / client registry / spawn lock)
    never touches the shared ``~/.clio``. The client ops run in a SUBPROCESS
    (:mod:`tests.test_arc._cte_offload_client`) so that (a) the process-global init
    guard stays clean and (b) clio-core#722 -- a native access-violation on ops
    against a dead runtime -- fails the subprocess (non-zero exit) instead of
    killing the pytest host. The subprocess only ever runs ops after the store's
    own connect-or-spawn has verified the daemon is bound; the parent force-reaps
    any surviving daemon after the subprocess exits.

Sabotage (recorded, not committed as a second test): rerunning the same flow with
``_RAM_CAP`` raised to ``"512MB"`` (no eviction) leaves the marker ABSENT from the
backing file, turning the offload assertion red -- confirming it is discriminating.

Marked ``integration`` (needs the clio-core binding + launcher); id contains
``cte`` so ``-k "not cte"`` deselects it in constrained envs.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("clio_cte_core_ext")

from clio_agent.arc import storage  # noqa: E402 - after importorskip

# ---- private-daemon tunables ------------------------------------------------
_TOTAL_MB = 30  # working-set size written through the store
_RAM_CAP = "2MB"  # ram-tier capacity_limit -> forces spill of ~28 MB to disk
_FILE_CAP = "128MB"  # disk-tier capacity (preallocated backing file)
_SUBPROCESS_TIMEOUT_S = 180.0

# A private clio-core config: DRAM hot tier (small cap) + file cold tier, plus an
# explicit ``networking.port`` so the daemon binds a port we control.
_PRIVATE_CONFIG = """\
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
        capacity_limit: "{ram_cap}"
        score: 1.0
      - path: "{file_tier}"
        bdev_type: "file"
        capacity_limit: "{file_cap}"
        score: 0.0
    dpe:
      dpe_type: "max_bw"
    performance:
      metadata_log_path: "{metadata_log}"
      transaction_log_capacity: "32MB"
"""


def _require_launcher() -> None:
    """Skip unless the clio-core standalone launcher (``clio_run``) is present.

    A private daemon cannot be spawned without it, so its absence is a skip, not a
    failure (mirrors the binding importorskip).
    """
    try:
        import iowarp_core  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - binding present but core missing
        pytest.skip(f"iowarp_core unavailable: {exc}")
    if storage._runtime_launcher_path(iowarp_core) is None:
        pytest.skip("clio-core launcher (clio_run) not found; cannot spawn a private daemon")


def _reserve_port_block(block: int = 5) -> int:
    """Return a base port with ``block`` consecutive free ports, below the ephemeral
    range.

    clio-core binds a CONTIGUOUS cluster of ports around ``networking.port`` (base,
    base+1, base+3). Picking a base in the ephemeral range risks base+N colliding
    with a transient connection -- which half-binds the daemon (the base port comes
    up, so a naive liveness probe passes) yet leaves it unhealthy, so client ops then
    access-violate. Choosing a base below the ephemeral floor and verifying the whole
    block binds at once avoids that failure mode.

    Args:
        block: Number of consecutive ports that must be free.

    Returns:
        The base port of a verified-free contiguous block.
    """
    import random  # noqa: PLC0415

    for _ in range(400):
        base = random.randint(20000, 40000)
        socks: list[socket.socket] = []
        ok = True
        for off in range(block):
            sock = socket.socket()
            try:
                sock.bind(("0.0.0.0", base + off))
                socks.append(sock)
            except OSError:
                ok = False
                break
        for sock in socks:
            sock.close()
        if ok:
            return base
    raise RuntimeError("could not reserve a free contiguous port block")


def _expected_needle(run_id: str) -> bytes:
    """Return the on-disk needle: the base64 encoding of the run marker.

    Mirrors :func:`tests.test_arc._cte_offload_client._marker` (the marker is padded
    to a multiple of 3 so ``base64(marker * k) == base64(marker) * k``), so a
    contiguous run of the marker on disk is a contiguous run of this needle.
    """
    marker = f"CLIO_CTE_OFFLOAD_{run_id}".encode()
    while len(marker) % 3:
        marker += b"_"
    return base64.b64encode(marker)


def _scan_for_needle(path: Path, needle: bytes) -> bool:
    """True if ``needle`` occurs anywhere in ``path`` (chunked read with overlap)."""
    if not path.is_file():
        return False
    overlap = len(needle)
    prev = b""
    with open(path, "rb") as handle:
        while True:
            block = handle.read(8 << 20)
            if not block:
                return False
            if needle in prev + block:
                return True
            prev = block[-overlap:]


def _reap_private_daemon(private_home: Path) -> None:
    """Force-reap the private daemon via its PRIVATE pidfile (best-effort).

    The subprocess's own atexit normally stops the daemon (last client out); this is
    the belt-and-suspenders reap so a crashed subprocess never leaks the daemon. It
    only ever touches the *private* home, never the shared ``~/.clio``.
    """
    pidfile = private_home / ".clio" / "clio-runtime.pid"
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
    except Exception:  # noqa: BLE001,S110 - already gone / psutil missing: best-effort
        pass


@pytest.mark.integration
def test_cte_backend_offloads_to_disk_and_reloads(tmp_path: Path) -> None:
    """A working set exceeding the CTE ram cap physically spills to the disk tier and
    reloads byte-identical, all through the real ``CTEStore`` path."""
    _require_launcher()

    private_home = tmp_path / "home"
    (private_home / ".clio").mkdir(parents=True)
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    file_tier = store_dir / "storage.bin"
    backing_file = store_dir / "storage.bin_node0"  # clio-core appends the node suffix

    port = _reserve_port_block()
    run_id = uuid.uuid4().hex[:8]
    config_path = tmp_path / "cte.yaml"
    config_path.write_text(
        _PRIVATE_CONFIG.format(
            port=port,
            conf_dir=conf_dir.as_posix(),
            ram_cap=_RAM_CAP,
            file_tier=file_tier.as_posix(),
            file_cap=_FILE_CAP,
            metadata_log=(store_dir / "metadata.log").as_posix(),
        ),
        encoding="utf-8",
    )

    out_path = tmp_path / "result.json"
    env = os.environ.copy()
    env.update(
        USERPROFILE=str(private_home),  # Windows Path.home()
        HOME=str(private_home),  # POSIX Path.home()
        CLIO_ARC_STORE="cte",
        CLIO_ARC_STORE_CONFIG=str(config_path),
        CLIO_SERVER_CONF=str(config_path),
        CLIO_CORE_PORT=str(port),
        CLIO_OFFLOAD_RUN_ID=run_id,
        CLIO_OFFLOAD_OUT=str(out_path),
        CLIO_OFFLOAD_TOTAL_MB=str(_TOTAL_MB),
        CTP_LOG_LEVEL="error",
    )
    client = Path(__file__).with_name("_cte_offload_client.py")

    try:
        proc = subprocess.run(  # noqa: S603 - fixed interpreter + in-repo script
            [sys.executable, str(client)],
            env=env,
            timeout=_SUBPROCESS_TIMEOUT_S,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    finally:
        # Give the subprocess's atexit teardown a beat, then force-reap any leftover.
        time.sleep(0.5)
        _reap_private_daemon(private_home)

    # (0) The subprocess must have exited cleanly. A non-zero code here is most likely
    # clio-core#722's access-violation (0xC0000005 == 3221225477) on a dead runtime --
    # exactly the crash we isolate so it FAILS the test instead of killing the host.
    assert proc.returncode == 0, (
        f"client subprocess failed rc={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr(tail):\n{proc.stderr[-2000:]}"
    )
    assert out_path.is_file(), "client subprocess did not write its result file"
    result = json.loads(out_path.read_text(encoding="utf-8"))

    # (1) The real store was used -- never a silent LocalFS fallback (which would make
    # the whole proof vacuous).
    assert result.get("store_type") == "CTEStore", f"not the CTE path: {result}"
    assert result.get("put_count", 0) >= 1 and result.get("total_bytes", 0) >= _TOTAL_MB * 1_000_000

    # (2) RELOAD: every blob read back through the store is byte-identical.
    assert result.get("readback_identical") is True, (
        f"CTE read-back was NOT byte-identical: mismatches={result.get('mismatches')}"
    )

    # (3) OFFLOAD (physical, non-vacuous): the marker bytes are in the disk backing
    # file -- cold blobs were spilled out of the 2 MB ram tier. Without a real spill
    # this needle is absent and the assertion fails (validated by the sabotage run).
    needle = _expected_needle(run_id)
    assert result.get("marker_b64", "").encode("ascii") == needle, "marker/needle mismatch"
    found = _scan_for_needle(backing_file, needle)
    if not found:  # fall back to any node-suffixed backing file
        for alt in store_dir.glob("storage.bin*"):
            if _scan_for_needle(alt, needle):
                found = True
                break
    assert found, (
        f"payload marker not found in the disk backing file {backing_file} -- the "
        f"working set did NOT physically offload to disk (backing file exists="
        f"{backing_file.is_file()}, size="
        f"{backing_file.stat().st_size if backing_file.is_file() else 'n/a'})"
    )
