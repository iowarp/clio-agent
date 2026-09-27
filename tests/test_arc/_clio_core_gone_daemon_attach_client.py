"""Subprocess client: the native attach against a private daemon that is GONE or STUCK.

``clio_init(kClient)`` holds the GIL for its whole wait for the runtime, so a daemon
that never answers the handshake freezes the interpreter for that long. This runs in a
subprocess so a regression hangs the CHILD and the parent's timeout fails the test.

Steps: spawn a private daemon (without attaching), then either kill it (``kill``: the
port answered liveness, then the daemon died -- a crash at startup) or suspend it
(``suspend``: alive but unresponsive), then build the ARC store, which attaches, and
write the outcome as JSON to ``$CLIO_STUCK_OUT``.
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
from typing import Any


def main() -> int:
    """Run the gone/stuck-daemon attach and report. Returns a process exit code."""
    out_path = os.environ["CLIO_STUCK_OUT"]
    mode = os.environ.get("CLIO_STUCK_MODE", "kill")
    result: dict[str, Any] = {"stage": "start", "mode": mode}
    daemon = None
    try:
        import iowarp_core
        import psutil

        from clio_agent.arc import storage
        from clio_agent.arc.init_degradation import arc_init_degradation_snapshot

        config_path = os.environ["CLIO_ARC_STORE_CONFIG"]
        port = storage._resolve_runtime_port(config_path)
        storage._spawn_runtime_daemon(iowarp_core, config_path, "error")
        deadline = time.monotonic() + 90
        while not storage._runtime_alive(port):
            if time.monotonic() > deadline:
                result["error"] = f"the private daemon never bound port {port}"
                return 3
            time.sleep(0.25)
        daemon = psutil.Process(
            int(storage._daemon_pidfile().read_text(encoding="utf-8").split()[0])
        )
        if mode == "kill":
            daemon.kill()
            daemon.wait(10)
            daemon = None
            # Liveness saw the port just before the daemon died: the attach proceeds.
            storage._runtime_alive = lambda _port: True
        else:
            daemon.suspend()
        result["stage"] = mode
        started = time.monotonic()
        store = storage.make_arc_store(backend="cte")
        result["elapsed_s"] = time.monotonic() - started
        result["store_type"] = type(store).__name__
        record = arc_init_degradation_snapshot()
        result["reason"] = getattr(record, "reason", None)
        result["error"] = str(getattr(record, "error", "") or "")
        return 0
    except Exception as exc:  # noqa: BLE001 - reported to the parent as a failure
        result["exception"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
        return 2
    finally:
        if daemon is not None:
            try:
                daemon.resume()
            except Exception as exc:  # noqa: BLE001 - recorded; the parent reaps anyway
                result["resume_error"] = str(exc)
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(result, handle)


if __name__ == "__main__":
    code = main()
    # Skip interpreter teardown against a daemon we just killed or stalled; the parent
    # reaps the private daemon.
    os._exit(code)
