"""Store writes and deletes stay bounded, and never freeze Python, against a stalled daemon.

``ClioCoreStore.put`` used the blocking ``Tag.PutBlob`` (and ``delete`` the blocking
``Client.DelBlob``). Those hold the GIL for the whole RPC, so against a suspended
daemon the interpreter froze: the per-RPC stall watch could not wake to fire its bound,
and the op waited as long as the daemon did. They now use ``AsyncPutBlob`` /
``AsyncDelBlob`` polled GIL-free (``clio_core_async_ops``): other threads keep running,
and the stall ladder degrades typed (``clio_core_rpc_stalled``) within its bound.

Hermetic: a private daemon on a reserved port with its own state dir, driven from a
subprocess; the daemon is always resumed and reaped.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("clio_cte_core_ext")

from tests.test_arc.test_clio_core_offload_spill import (  # noqa: E402 - after importorskip
    _FILE_CAP,
    _PRIVATE_CONFIG,
    _RAM_CAP,
    _reap_private_daemon,
    _require_launcher,
    _reserve_port_block,
)

_STALL_S = 2.0  # arc.liveness.stall_after_s for the child
_CHILD_TIMEOUT_S = 180.0


@pytest.mark.parametrize("op", ["put", "delete"])
def test_store_writes_are_bounded_and_gil_free_against_a_suspended_daemon(
    tmp_path: Path, op: str
) -> None:
    _require_launcher()
    private_home = tmp_path / "home"
    (private_home / ".clio").mkdir(parents=True)
    (tmp_path / "conf").mkdir()
    (tmp_path / "store").mkdir()
    port = _reserve_port_block()
    config_path = tmp_path / "cte.yaml"
    config_path.write_text(
        _PRIVATE_CONFIG.format(
            port=port,
            conf_dir=(tmp_path / "conf").as_posix(),
            ram_cap=_RAM_CAP,
            file_tier=(tmp_path / "store" / "storage.bin").as_posix(),
            file_cap=_FILE_CAP,
            metadata_log=(tmp_path / "store" / "metadata.log").as_posix(),
        ),
        encoding="utf-8",
    )
    out_path = tmp_path / "result.json"
    env = os.environ.copy()
    env.update(
        USERPROFILE=str(private_home),
        HOME=str(private_home),
        CLIO_RUNTIME_STATE_DIR=str(private_home / ".clio"),
        CLIO_ARC_STORE="cte",
        CLIO_ARC_STORE_CONFIG=str(config_path),
        CLIO_SERVER_CONF=str(config_path),
        CLIO_CORE_PORT=str(port),
        CLIO_ARC_LIVENESS_STALL_AFTER_S=str(_STALL_S),
        CLIO_ARC_LIVENESS_RETRIES="0",
        CLIO_STUCK_OUT=str(out_path),
        CLIO_STUCK_OP=op,
        CTP_LOG_LEVEL="error",
    )
    client = Path(__file__).with_name("_clio_core_stalled_put_client.py")
    log_path = tmp_path / "client.log"  # a file, not a pipe: the daemon inherits handles
    try:
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.run(  # noqa: S603 - fixed interpreter + in-repo script
                [sys.executable, str(client)],
                env=env,
                timeout=_CHILD_TIMEOUT_S,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"the store op hung past {_CHILD_TIMEOUT_S:g}s (a GIL-holding call):"
            + chr(10)
            + log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        )
    finally:
        time.sleep(0.5)
        _reap_private_daemon(private_home)
    output = log_path.read_text(encoding="utf-8", errors="replace")

    assert out_path.is_file(), f"no result; rc={proc.returncode}\n{output[-3000:]}"
    result = json.loads(out_path.read_text(encoding="utf-8"))
    assert proc.returncode == 0, f"client failed: {result}\n{output[-3000:]}"
    row = result["op"]
    # The stall ladder fired, typed, within its bound (never a hang)...
    assert row["result"] == "ClioCoreRuntimeLostError", row
    assert row["reason"] == "clio_core_rpc_stalled", row
    assert row["elapsed_s"] < _STALL_S + 8.0, row
    # ...and the interpreter kept running the whole time (a GIL-holding call gives ~0).
    assert row["heartbeats"] > 50, row
