"""The CLIO server survives the native clio-core client's exit paths during the attach.

The native client ends the process with ``exit(1)`` (``HLOG(kFatal, ...)``) on some
startup failures instead of returning. Before the guard, both setups below ended the
attaching process within a second of ``clio_init``, with no typed error and no
result (reproduced against iowarp-core 2.2.1):

- ``with_runtime``: an inherited ``CLIO_WITH_RUNTIME=1`` makes the native client start
  its own runtime, which collides on the port with the host daemon another process
  owns (``FATAL ... kill -9 <PID>``);
- ``bad_config``: the daemon's recorded config, which the attach adopts, was edited
  after the daemon started into a value yaml-cpp cannot convert (``FATAL ...
  LoadFromFile yaml-cpp: ... bad conversion``).

Now the first is removed before the attach (recorded) and the attach succeeds; the
second raises the typed ``clio_core_native_client_exit`` store error and the process
lives. Hermetic: private daemon, reserved port, own state dir, driven from a subprocess.
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

_CHILD_TIMEOUT_S = 180.0


def _durable(config_text: str) -> str:
    """The shared private config with its file tier marked durable: clio-core refuses a
    config that would forget everything on restart before it ever attaches."""
    marker = '        bdev_type: "file"\n'
    assert config_text.count(marker) == 1
    return config_text.replace(marker, marker + '        persistence_level: "temporary"\n')


def _write_config(path: Path, *, port: int, root: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    (root / "conf").mkdir(parents=True, exist_ok=True)
    (root / "store").mkdir(parents=True, exist_ok=True)
    path.write_text(
        _durable(
            _PRIVATE_CONFIG.format(
                port=port,
                conf_dir=(root / "conf").as_posix(),
                ram_cap=_RAM_CAP,
                file_tier=(root / "store" / "storage.bin").as_posix(),
                file_cap=_FILE_CAP,
                metadata_log=(root / "store" / "metadata.log").as_posix(),
            )
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("mode", ["with_runtime", "bad_config"])
def test_the_native_client_cannot_exit_the_attaching_process(tmp_path: Path, mode: str) -> None:
    _require_launcher()
    private_home = tmp_path / "home"
    (private_home / ".clio").mkdir(parents=True)
    port = _reserve_port_block()
    # The daemon runs its own config (recorded); this process asks for another one on
    # the same port, so the attach adopts the daemon's (first config wins).
    daemon_config = _write_config(
        tmp_path / "daemon" / "cte.yaml", port=port, root=tmp_path / "daemon"
    )
    requested = _write_config(tmp_path / "mine" / "cte.yaml", port=port, root=tmp_path / "mine")
    out_path = tmp_path / "result.json"
    env = os.environ.copy()
    env.pop("CLIO_WITH_RUNTIME", None)
    env.update(
        USERPROFILE=str(private_home),
        HOME=str(private_home),
        CLIO_RUNTIME_STATE_DIR=str(private_home / ".clio"),
        CLIO_ARC_STORE="cte",
        CLIO_ARC_STORE_CONFIG=str(requested),
        CLIO_SERVER_CONF=str(requested),
        CLIO_CORE_PORT=str(port),
        CLIO_EXIT_DAEMON_CONFIG=str(daemon_config),
        CLIO_EXIT_OUT=str(out_path),
        CLIO_EXIT_MODE=mode,
        CTP_LOG_LEVEL="error",
    )
    client = Path(__file__).with_name("_clio_core_native_exit_client.py")
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
            f"the attach hung past {_CHILD_TIMEOUT_S:g}s:"
            + chr(10)
            + log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        )
    finally:
        time.sleep(0.5)
        _reap_private_daemon(private_home)
    output = log_path.read_text(encoding="utf-8", errors="replace")

    assert out_path.is_file(), (
        f"the native client ended the attaching process (rc={proc.returncode}):\n{output[-3000:]}"
    )
    result = json.loads(out_path.read_text(encoding="utf-8"))
    assert proc.returncode == 0, f"client failed: {result}\n{output[-3000:]}"
    if mode == "with_runtime":
        assert result["store_type"] == "ClioCoreStore", result
        assert result["removed_env"] == {"CLIO_WITH_RUNTIME": "1"}, result
        assert result["with_runtime_after"] is None, result
    else:
        assert result.get("raised") == "ArcStoreUnavailableError", result
        assert result["reason"] == "clio_core_native_client_exit", result
        assert "bad conversion" in result["error"], result
