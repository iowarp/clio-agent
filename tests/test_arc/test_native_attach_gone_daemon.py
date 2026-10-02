"""The native client attach is bounded by CLIO's liveness policy against a gone or stuck daemon.

``clio_init(kClient)`` waits for the runtime to answer its ``ClientConnect`` handshake,
and holds the GIL for the whole wait. The wait was the native default (30 s, from
``CLIO_WAIT_SERVER``), whatever CLIO was configured with: a daemon that crashed right
after binding its port (``kill``), or one alive but unresponsive (``suspend``), froze
the interpreter for 30 s and then failed with the generic
``clio_core_client_attach_failed``. The attach now hands the native client CLIO's own
bound (``arc.liveness.stall_after_s``) and an expired bound fails with the typed
``clio_core_client_attach_timeout``.

Hermetic: a private daemon on a reserved port with its own state dir, driven from a
subprocess (a regression hangs the CHILD; the parent's timeout fails the test). The
daemon is always resumed and reaped.
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


def _durable(config_text: str) -> str:
    """The shared private config with its file tier marked durable: clio-core refuses a
    config that would forget everything on restart before it ever attaches."""
    marker = '        bdev_type: "file"\n'
    assert config_text.count(marker) == 1
    return config_text.replace(marker, marker + '        persistence_level: "temporary"\n')


_WINDOW_S = 3.0  # arc.liveness.stall_after_s for the child: the attach's bound
_NATIVE_DEFAULT_S = 30.0  # what the native client waits when nobody tells it otherwise
_CHILD_TIMEOUT_S = 150.0  # spawn + attach headroom; a GIL hang blows through it


@pytest.mark.parametrize("mode", ["kill", "suspend"])
def test_native_attach_is_bounded_against_a_gone_or_stuck_daemon(tmp_path: Path, mode: str) -> None:
    """``kill``: the daemon died after liveness saw its port; ``suspend``: alive, silent."""
    _require_launcher()
    private_home = tmp_path / "home"
    (private_home / ".clio").mkdir(parents=True)
    (tmp_path / "conf").mkdir()
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    port = _reserve_port_block()
    config_path = tmp_path / "cte.yaml"
    config_path.write_text(
        _durable(
            _PRIVATE_CONFIG.format(
                port=port,
                conf_dir=(tmp_path / "conf").as_posix(),
                ram_cap=_RAM_CAP,
                file_tier=(store_dir / "storage.bin").as_posix(),
                file_cap=_FILE_CAP,
                metadata_log=(store_dir / "metadata.log").as_posix(),
            )
        ),
        encoding="utf-8",
    )
    out_path = tmp_path / "result.json"
    env = os.environ.copy()
    env.pop("CLIO_WAIT_SERVER", None)  # the bound must come from CLIO's own config
    env.update(
        USERPROFILE=str(private_home),
        HOME=str(private_home),
        CLIO_RUNTIME_STATE_DIR=str(private_home / ".clio"),
        CLIO_ARC_STORE="cte",
        CLIO_ARC_STORE_CONFIG=str(config_path),
        CLIO_SERVER_CONF=str(config_path),
        CLIO_CORE_PORT=str(port),
        CLIO_ARC_LIVENESS_STALL_AFTER_S=str(_WINDOW_S),
        # The failed attach stops the daemon it no longer needs; a suspended one makes no
        # progress, so it is killed after this stretch (15 s in production).
        CLIO_ARC_LIVENESS_STOP_NO_PROGRESS_S=str(_WINDOW_S),
        CLIO_STUCK_OUT=str(out_path),
        CLIO_STUCK_MODE=mode,
        CTP_LOG_LEVEL="error",
    )
    client = Path(__file__).with_name("_clio_core_gone_daemon_attach_client.py")
    # Output to FILES, not pipes: the daemon the child spawns inherits its handles.
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
            f"the attach hung past {_CHILD_TIMEOUT_S:g}s:"
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
    assert result.get("raised") == "ArcStoreUnavailableError", result  # typed, no other store
    # CLIO's bound, not the native 30 s default (and not a hang).
    assert result["elapsed_s"] < _WINDOW_S + 12.0 < _NATIVE_DEFAULT_S, result
    if result["elapsed_s"] >= _WINDOW_S:
        # The whole bound ran out: the typed timeout (Windows TCP, and any stuck daemon).
        assert result["reason"] == "clio_core_client_attach_timeout", result
        assert f"no answer within {_WINDOW_S:g}s" in result["error"], result
        if mode == "suspend":  # alive but silent: located, and seen making no progress
            assert "wait=no_progress" in result["error"], result
    else:
        # The native client gave up sooner (a refused connect to a gone daemon, as over
        # Linux's IPC socket): typed as a failed attach, never as a timeout.
        assert mode == "kill", result
        assert result["reason"] == "clio_core_client_attach_failed", result
