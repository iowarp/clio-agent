"""Subprocess client: a store write against a SUSPENDED daemon must not freeze the interpreter.

The blocking ``Tag.PutBlob`` held the GIL for the whole RPC, so while it waited on a
stalled daemon no other thread ran -- including the stall watch meant to bound it.
This attaches a real store, suspends the daemon, runs ``put`` or ``delete`` on a worker
thread while the main thread counts 10 ms heartbeats, and writes the outcome to
``$CLIO_STUCK_OUT``. Run as a subprocess: a regression hangs the CHILD (the parent's
timeout fails the test instead of freezing pytest).
"""

from __future__ import annotations

import socket as _socket

# Winsock must be initialised before the native binding loads (#914).
_probe = _socket.socket()
_probe.close()

import json
import os
import threading
import time
import traceback
from typing import Any


def _measure(op: Any) -> dict[str, Any]:
    outcome: dict[str, Any] = {}
    done = threading.Event()

    def worker() -> None:
        try:
            op()
            outcome["result"] = "completed"
        except Exception as exc:  # noqa: BLE001 - the typed degrade is the expected outcome
            outcome["result"] = type(exc).__name__
            outcome["reason"] = (getattr(exc, "details", None) or {}).get("reason", "")
        done.set()

    started = time.monotonic()
    threading.Thread(target=worker, daemon=True).start()
    beats = 0
    while not done.is_set() and time.monotonic() - started < 60:
        time.sleep(0.01)
        beats += 1
    outcome["elapsed_s"] = time.monotonic() - started
    outcome["heartbeats"] = beats
    return outcome


def main() -> int:
    """Run the stalled-daemon write and report. Returns a process exit code."""
    out_path = os.environ["CLIO_STUCK_OUT"]
    result: dict[str, Any] = {"stage": "start"}
    daemon = None
    try:
        import psutil

        from clio_agent.arc.storage import ClioCoreStore, _daemon_pidfile, make_arc_store

        store = make_arc_store(backend="cte")
        result["store_type"] = type(store).__name__
        if not isinstance(store, ClioCoreStore):
            return 3
        store.put("records", "warm-up", b"x")  # resolves the kind's tag id while live
        daemon = psutil.Process(int(_daemon_pidfile().read_text(encoding="utf-8").split()[0]))
        daemon.suspend()
        result["stage"] = "suspended"
        # One op per process: the first stall quarantines the store (by design), so a
        # second op would be refused at once instead of exercising its own RPC.
        if os.environ["CLIO_STUCK_OP"] == "put":
            result["op"] = _measure(lambda: store.put("records", "stalled", b"y" * 64))
        else:
            result["op"] = _measure(lambda: store.delete("records", "warm-up"))
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
    os._exit(code)  # skip teardown against a daemon we stalled; the parent reaps it
