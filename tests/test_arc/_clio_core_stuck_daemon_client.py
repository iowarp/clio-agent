"""Subprocess client: the post-attach probe against a SUSPENDED private clio-core daemon.

The clio-core binding's blocking calls hold the GIL for the whole RPC, so a daemon that
never answers freezes the entire interpreter. That is why this runs in a subprocess: if
the probe ever regresses to a blocking call, the child hangs and the parent's
``subprocess.run(timeout=...)`` fails the test instead of freezing pytest.

Steps: attach a real ``ClioCoreStore`` to a private daemon (env-configured by the
parent), suspend the daemon process, run ``verify_post_attach`` against it, resume the
daemon, and write the outcome as JSON to ``$CLIO_STUCK_OUT``.
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
    """Run the stuck-daemon probe and report. Returns a process exit code."""
    out_path = os.environ["CLIO_STUCK_OUT"]
    result: dict[str, Any] = {"stage": "start"}
    daemon = None
    try:
        import psutil

        from clio_agent.arc import clio_core_attach
        from clio_agent.arc.rpc_liveness import health_probe_window_s
        from clio_agent.arc.storage import ClioCoreStore, _daemon_pidfile, make_arc_store

        store = make_arc_store(backend="cte")
        result["store_type"] = type(store).__name__
        if not isinstance(store, ClioCoreStore):
            result["error"] = f"expected ClioCoreStore, got {type(store).__name__}"
            return 3
        daemon = psutil.Process(int(_daemon_pidfile().read_text(encoding="utf-8").split()[0]))
        result["window_s"] = health_probe_window_s()

        daemon.suspend()
        result["stage"] = "suspended"
        deregistered: list[bool] = []
        started = time.monotonic()
        try:
            clio_core_attach.verify_post_attach(store, on_failure=lambda: deregistered.append(True))
            result["outcome"] = "answered"
        except clio_core_attach.ClioCoreAttachError as exc:
            result["outcome"] = "typed_error"
            result["stage_name"] = exc.stage
            result["message"] = str(exc)
        result["elapsed_s"] = time.monotonic() - started
        result["deregistered"] = deregistered
        return 0
    except Exception as exc:  # noqa: BLE001 - reported to the parent as a failure
        result["error"] = f"{type(exc).__name__}: {exc}"
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
    # Skip interpreter teardown (atexit release) against a daemon we just stalled; the
    # parent reaps the private daemon.
    os._exit(code)
