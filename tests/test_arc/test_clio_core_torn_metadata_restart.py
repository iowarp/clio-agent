"""Records written after a torn metadata record survive the next restarts (FN-14).

The companion of ``test_clio_core_restart_durability`` (a clean stop keeps a record).

A clio-core daemon killed while it appended to its metadata log leaves the last record
torn. iowarp-core 2.4.1 ignores the torn record on its next start, then appends after
the torn bytes, so from the start after that the reader stops inside the new records:
everything written since the tear is gone, start after start. CLIO cuts the torn
record out before it spawns the daemon (:mod:`clio_agent.arc.clio_core_wal_repair`).

Real clio-core, hermetic: a private daemon on a reserved port, its own state directory
and store, one subprocess per daemon lifetime (write, stop; tear; write, stop; check).

**Sabotage:** drop the ``repair_metadata_logs`` call from ``storage._spawn_runtime_daemon``
-> the second batch is missing at the final check.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("clio_cte_core_ext")

from clio_agent.arc import clio_core_config  # noqa: E402 - after importorskip
from clio_agent.arc.clio_core_wal_repair import MAGIC, scan_log  # noqa: E402
from tests.test_arc.test_clio_core_offload_spill import (  # noqa: E402
    _reap_private_daemon,
    _require_launcher,
    _reserve_port_block,
)

_CHILD_TIMEOUT_S = 150.0
_COUNT = 30


def _tear_last_record(store_dir: Path) -> Path:
    """Cut the most recently written record inside itself, as a kill mid-append leaves it."""
    newest: tuple[int, Path, int, int] | None = None
    for log in store_dir.glob("metadata.log.blob.?"):
        data = log.read_bytes()
        assert data[:4] == MAGIC
        offset, last, seq = 4, None, -1
        while offset < len(data):
            _kind, seq, length = struct.unpack_from("<BQI", data, offset)
            last, offset = offset, offset + 13 + length
        assert offset == len(data), "the log was not whole before the tear"
        if last is not None and (newest is None or seq > newest[0]):
            newest = (seq, log, last, len(data))
    assert newest is not None
    _seq, target, last, size = newest
    os.truncate(target, last + 13 + (size - last - 13) // 2)
    return target


def _private_store(tmp_path: Path) -> tuple[Path, Any]:
    """A private clio-core config and a runner: one subprocess per daemon lifetime."""
    _require_launcher()
    home = tmp_path / "home"
    (home / ".clio").mkdir(parents=True)
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    (tmp_path / "conf").mkdir()
    port = _reserve_port_block()
    config = tmp_path / "cte.yaml"
    config.write_text(
        clio_core_config._DEFAULT_CTE_CONFIG_TEMPLATE.format(
            core_port=port,
            core_threads=2,
            conf_dir=(tmp_path / "conf").as_posix(),
            ram_budget="256MB",
            file_tier=(store_dir / "storage.bin").as_posix(),
            file_capacity="32MB",
            metadata_log=(store_dir / "metadata.log").as_posix(),
            indexer="",
        ),
        encoding="utf-8",
    )
    out = tmp_path / "result.json"
    env = os.environ.copy()
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        CLIO_RUNTIME_STATE_DIR=str(home / ".clio"),
        CLIO_ARC_STORE="cte",
        CLIO_ARC_STORE_CONFIG=str(config),
        CLIO_CORE_PORT=str(port),
        CLIO_DURABILITY_OUT=str(out),
        CLIO_STORE_OPS_OUT=str(out),
        CTP_LOG_LEVEL="error",
    )
    runs = iter(range(1000))

    def run(client: str, *args: str) -> dict[str, Any]:
        log = tmp_path / f"client-{next(runs)}.log"
        out.unlink(missing_ok=True)
        try:
            with log.open("w", encoding="utf-8") as handle:
                proc = subprocess.run(  # noqa: S603 - fixed interpreter + in-repo script
                    [sys.executable, str(Path(__file__).with_name(client)), *args],
                    env=env,
                    timeout=_CHILD_TIMEOUT_S,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    cwd=Path(__file__).parents[2],
                )
        finally:
            time.sleep(0.5)
            _reap_private_daemon(home)
        text = log.read_text(encoding="utf-8", errors="replace")
        assert proc.returncode == 0 and out.is_file(), text[-3000:]
        return json.loads(out.read_text(encoding="utf-8"))

    return store_dir, run


@pytest.mark.timeout(600)  # four daemon starts and stops, each up to a minute on CI
def test_records_written_after_a_torn_metadata_record_survive_restarts(tmp_path: Path) -> None:
    store_dir, run_client = _private_store(tmp_path)

    def run(*args: str) -> dict[str, Any]:
        return run_client("_restart_durability_client.py", *args)

    run("write", "before", str(_COUNT))
    torn = _tear_last_record(store_dir)
    assert scan_log(torn).verdict == "cut"
    run("write", "after", str(_COUNT))  # this start cuts the torn record out first

    after = run("check", "after", str(_COUNT))
    assert after["missing"] == [], after
    before = run("check", "before", str(_COUNT))
    assert len(before["missing"]) <= 1, before  # at most the record that was torn
    assert all(scan_log(path).verdict == "clean" for path in store_dir.glob("metadata.log.*.?"))
    assert any(store_dir.glob("metadata.log.blob.*.torn-*")), "the cut bytes are kept"


@pytest.mark.timeout(600)  # five daemon starts and stops
def test_a_torn_delete_never_hands_the_restored_blobs_block_to_a_new_blob(
    tmp_path: Path,
) -> None:
    """The allocation half: the delete's free reached the allocation log, its metadata
    record did not. The restored blob still points at that block; a new blob given it
    was overwritten by the restored blob's next write (read back as its bytes).

    **Sabotage:** skip ``withhold_trailing_frees`` in ``repair_metadata_logs`` -> the new
    record reads back as ``MARKER-REWRITTEN``.
    """
    store_dir, run_client = _private_store(tmp_path)

    def run(*ops: str) -> dict[str, Any]:
        return run_client("_store_ops_client.py", *ops)

    run("put:keep:KEEP", "put:marker:MARKER-ORIGINAL-BYTES")
    run("del:marker")
    torn = _tear_last_record(store_dir)  # the delete is the last record written
    assert scan_log(torn).torn_type == 3

    run("put:conversation:NEW-CONVERSATION-RECORD")
    run("put:marker:MARKER-REWRITTEN")
    reads = run("get:conversation", "get:marker", "get:keep")

    assert reads == {
        "conversation": "NEW-CONVERSATION-RECORD",
        "marker": "MARKER-REWRITTEN",
        "keep": "KEEP",
    }
    assert any(store_dir.glob("storage.bin_node0.alloc_log.withheld-*")), "frees kept aside"
