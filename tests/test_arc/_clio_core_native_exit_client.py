"""Subprocess client: attach to a host daemon in the two setups where the native client exits.

The clio-core native client calls ``exit(1)`` on some startup failures instead of
returning. This runs as its own process so that, before the guard, the exit ends this
CHILD (the parent sees no result file) rather than pytest.

A private daemon is spawned first (without attaching: "owned by another process" from
the attach's point of view), then, per ``$CLIO_EXIT_MODE``:

- ``with_runtime``: ``CLIO_WITH_RUNTIME=1`` is set, so the native client would start
  its own runtime, collide with the daemon on the port, and exit;
- ``bad_config``: the daemon's recorded config (the one the attach adopts: first config
  wins) is edited after it started into a value yaml-cpp cannot convert, so the native
  config loader would exit.

The ARC store is then built and the outcome written as JSON to ``$CLIO_EXIT_OUT``.
"""

from __future__ import annotations

import socket as _socket

# Winsock must be initialised before the native binding loads (#914).
_probe = _socket.socket()
_probe.close()

import json
import os
import time
import traceback
from pathlib import Path
from typing import Any


def main() -> int:
    """Run the attach and report. Returns a process exit code."""
    out_path = os.environ["CLIO_EXIT_OUT"]
    mode = os.environ["CLIO_EXIT_MODE"]
    result: dict[str, Any] = {"stage": "start", "mode": mode}
    try:
        import iowarp_core

        from clio_agent.arc import storage
        from clio_agent.arc.clio_core_native_preflight import removed_embedded_runtime_env
        from clio_agent.arc.init_degradation import arc_init_degradation_snapshot

        daemon_config = os.environ["CLIO_EXIT_DAEMON_CONFIG"]
        port = storage._resolve_runtime_port(daemon_config)
        storage._spawn_runtime_daemon(iowarp_core, daemon_config, "error")
        deadline = time.monotonic() + 90
        while not storage._runtime_alive(port):
            if time.monotonic() > deadline:
                result["error"] = f"the private daemon never bound port {port}"
                return 3
            time.sleep(0.25)
        if mode == "with_runtime":
            os.environ["CLIO_WITH_RUNTIME"] = "1"
        else:
            text = Path(daemon_config).read_text(encoding="utf-8")
            Path(daemon_config).write_text(
                text.replace("num_threads: 2", "num_threads: two"), encoding="utf-8"
            )
        result["stage"] = "attach"
        store = storage.make_arc_store(backend="cte")
        result["store_type"] = type(store).__name__
        record = arc_init_degradation_snapshot()
        result["reason"] = getattr(record, "reason", None)
        result["error"] = str(getattr(record, "error", "") or "")
        result["removed_env"] = removed_embedded_runtime_env()
        result["with_runtime_after"] = os.environ.get("CLIO_WITH_RUNTIME")
        return 0
    except Exception as exc:  # noqa: BLE001 - reported to the parent as a failure
        result["exception"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
        return 2
    finally:
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(result, handle)


if __name__ == "__main__":
    code = main()
    # The parent reaps the private daemon; skip teardown against it.
    os._exit(code)
