"""The post-attach probe stays bounded against a SUSPENDED (stuck) clio-core daemon.

Regression for the #1439 CI hang: the first post-attach probe issued a blocking binding
call (``Tag(...)`` + ``GetBlobSize``). The binding holds the GIL for the whole RPC, so a
daemon that did not answer froze the entire interpreter, stall watchers included, until
the 6-hour job timeout. The probe now waits on the async ``Future`` with a bound derived
from the liveness policy; this test suspends a real private daemon and proves the probe
returns a typed error within that bound.

Hermetic: a private daemon on a reserved port with its own state dir, driven from a
subprocess (a regression hangs the CHILD, and ``subprocess.run(timeout=...)`` fails the
test instead of freezing pytest). The daemon is always resumed and reaped.
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

_WINDOW_S = 3.0  # arc.liveness.stall_after_s for the child: the probe's bound
_CHILD_TIMEOUT_S = 150.0  # attach + spawn headroom; a GIL hang blows through it


@pytest.mark.parametrize("mode", ["suspend", "kill"])
def test_post_attach_probe_is_bounded_against_a_stuck_daemon(tmp_path: Path, mode: str) -> None:
    """``suspend``: alive but unresponsive; ``kill``: gone (CI: a daemon that died at startup
    left the probe's ``Future.wait(max_sec)`` blocking past its bound)."""
    _require_launcher()
    private_home = tmp_path / "home"
    (private_home / ".clio").mkdir(parents=True)
    (tmp_path / "conf").mkdir()
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    port = _reserve_port_block()
    config_path = tmp_path / "cte.yaml"
    config_path.write_text(
        _PRIVATE_CONFIG.format(
            port=port,
            conf_dir=(tmp_path / "conf").as_posix(),
            ram_cap=_RAM_CAP,
            file_tier=(store_dir / "storage.bin").as_posix(),
            file_cap=_FILE_CAP,
            metadata_log=(store_dir / "metadata.log").as_posix(),
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
        CLIO_ARC_LIVENESS_STALL_AFTER_S=str(_WINDOW_S),
        CLIO_STUCK_OUT=str(out_path),
        CLIO_STUCK_MODE=mode,
        CTP_LOG_LEVEL="error",
    )
    client = Path(__file__).with_name("_clio_core_stuck_daemon_client.py")
    # Output to FILES, not pipes: a daemon the child spawns inherits its handles, and a
    # pipe it holds open would keep ``subprocess.run`` waiting after the child exited.
    log_path = tmp_path / "client.log"
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
            f"the probe hung past {_CHILD_TIMEOUT_S:g}s:"
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
    assert result["store_type"] == "ClioCoreStore", result
    assert result["window_s"] == _WINDOW_S
    assert result["outcome"] == "typed_error", result
    assert result["stage_name"] == "post_attach_probe"
    assert result["reason"] == "clio_core_post_attach_probe_timeout"
    assert f"did not answer within {_WINDOW_S:g}s" in result["message"]
    assert result["elapsed_s"] < _WINDOW_S + 2.0, result  # the bound, not a hang
    assert result["deregistered"] == [True]
